bl_info = {
    "name": "HDR Encoding Tools",
    "author": "Theanine3D",
    "version": (1, 3, 0),
    "blender": (5, 0, 0),
    "location": "UV/Image Editor > Sidebar (N) > HDR Tools",
    "description": "Tools for encoding / compressing HDR images and vertex colors, for use in game engines",
    "category": "UV",
}

import os
from collections import deque

import bpy
import numpy as np
from bpy.props import (EnumProperty, FloatProperty, PointerProperty,
                       StringProperty)
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper

# Unity conventions: lightmap data is stored gamma-encoded (1/2.2).
# RGBM covers a gamma-space range of [0, 5]  -> linear [0, 5^2.2  = 34.49]
# dLDR covers a gamma-space range of [0, 2]  -> linear [0, 2^2.2  =  4.59]
GAMMA = 1.0 / 2.2
RGBM_RANGE = 5.0
DLDR_RANGE = 2.0

VALID_EXTS = (".exr", ".hdr")


def encode_rgbm(rgb_linear):
    """linear float RGB -> (rgb, m) in [0,1], decode: (rgb * m * 5)^2.2"""
    gamma = np.power(np.clip(rgb_linear, 0.0, None), GAMMA)
    rgbm = np.clip(gamma / RGBM_RANGE, 0.0, 1.0)
    m = np.max(rgbm, axis=-1)
    m = np.clip(m, 1.0 / 255.0, 1.0)
    # Quantize the multiplier up to the next 8-bit step so rgb/m never exceeds 1
    m = np.ceil(m * 255.0) / 255.0
    rgb = np.clip(rgbm / m[..., None], 0.0, 1.0)
    return rgb, m


def encode_dldr(rgb_linear):
    """linear float RGB -> [0,1], decode: (value * 2) in gamma space"""
    gamma = np.power(np.clip(rgb_linear, 0.0, None), GAMMA)
    return np.clip(gamma / DLDR_RANGE, 0.0, 1.0)


def convert_hdr_file(src_path, encoding):
    """Encode one .exr/.hdr file to a PNG saved next to it.

    Returns the new Blender image datablock; raises RuntimeError on failure.
    """
    src = bpy.data.images.load(src_path, check_existing=False)
    try:
        width, height = src.size
        if width == 0 or height == 0:
            raise RuntimeError("image has no pixel data")

        pixels = np.empty(width * height * 4, dtype=np.float32)
        src.pixels.foreach_get(pixels)
        rgb_linear = pixels.reshape(-1, 4)[:, :3]

        out = np.empty((width * height, 4), dtype=np.float32)
        if encoding == 'RGBM':
            out[:, :3], out[:, 3] = encode_rgbm(rgb_linear)
            suffix = "_RGBM"
        else:
            out[:, :3] = encode_dldr(rgb_linear)
            out[:, 3] = 1.0
            suffix = "_dLDR"
    finally:
        bpy.data.images.remove(src)

    out_path = os.path.splitext(src_path)[0] + suffix + ".png"
    out_name = os.path.basename(out_path)

    # Replace any previous result so we don't accumulate .001 duplicates
    existing = bpy.data.images.get(out_name)
    if existing is not None:
        bpy.data.images.remove(existing)

    img = bpy.data.images.new(out_name, width, height,
                              alpha=True, float_buffer=False)
    # Alpha carries the RGBM multiplier, not transparency
    img.alpha_mode = 'CHANNEL_PACKED'
    img.colorspace_settings.name = 'sRGB'
    img.pixels.foreach_set(out.ravel())

    img.filepath_raw = out_path
    img.file_format = 'PNG'
    try:
        img.save()
    except RuntimeError:
        bpy.data.images.remove(img)
        raise
    return img


def selected_unique_meshes(context):
    """Yield one (object, mesh) pair per unique editable mesh in the
    selection, so multi-user meshes are only processed once."""
    seen = set()
    for obj in context.selected_objects:
        if obj.type != 'MESH':
            continue
        mesh = obj.data
        if mesh.library is not None or mesh.name in seen:
            continue
        seen.add(mesh.name)
        yield obj, mesh


def scale_color_attribute(attr, factor):
    """Multiply the RGB of every element of a color attribute; alpha
    is left untouched."""
    count = len(attr.data)
    if count == 0:
        return
    buf = np.empty(count * 4, dtype=np.float32)
    attr.data.foreach_get("color", buf)
    rgba = buf.reshape(-1, 4)
    rgba[:, :3] *= factor
    attr.data.foreach_set("color", buf)


def convert_color_attribute_type(context, obj, data_type):
    """Convert every color attribute on obj's mesh to Face Corner domain
    and the given data type. Domain is always forced to Face Corner
    (Unity's FBX import expects it); only the data type toggles between
    compress and decompress. Returns the number of layers now in that
    format."""
    mesh = obj.data
    names = [a.name for a in mesh.color_attributes]
    converted = 0
    view_layer = context.view_layer
    prev_active = view_layer.objects.active
    view_layer.objects.active = obj
    try:
        for name in names:
            idx = mesh.color_attributes.find(name)
            if idx < 0:
                continue
            attr = mesh.color_attributes[idx]
            if attr.domain == 'CORNER' and attr.data_type == data_type:
                converted += 1
                continue
            mesh.color_attributes.active_color_index = idx
            bpy.ops.geometry.color_attribute_convert(
                domain='CORNER', data_type=data_type)
            converted += 1
    finally:
        view_layer.objects.active = prev_active
    return converted


def fix_buried_vertices(mesh, attr, threshold):
    """Replace the color of every buried vertex — one whose RGB channels
    are all at or below the darkness threshold — with the color of the
    nearest connected non-buried vertex (breadth-first over mesh edges,
    so a whole dark patch is filled from its border inward).
    Works on both Vertex and Face Corner domains; alpha is untouched.
    Returns (fixed, unreachable) vertex counts."""
    count = len(attr.data)
    n_verts = len(mesh.vertices)
    if count == 0 or n_verts == 0:
        return 0, 0

    buf = np.empty(count * 4, dtype=np.float32)
    attr.data.foreach_get("color", buf)
    rgba = buf.reshape(-1, 4)

    if attr.domain == 'CORNER':
        loop_v = np.empty(len(mesh.loops), dtype=np.int64)
        mesh.loops.foreach_get("vertex_index", loop_v)
        corner_nonblack = rgba[:, :3].max(axis=1) > threshold
        # A vertex is buried only if every one of its corners is dark.
        # A non-buried vertex's replacement color averages only its
        # non-dark corners, so seam vertices don't contribute darkness.
        col_sum = np.zeros((n_verts, 3), dtype=np.float64)
        col_cnt = np.zeros(n_verts, dtype=np.int64)
        np.add.at(col_sum, loop_v[corner_nonblack],
                  rgba[corner_nonblack, :3])
        np.add.at(col_cnt, loop_v[corner_nonblack], 1)
        has_corner = np.zeros(n_verts, dtype=bool)
        has_corner[loop_v] = True
        source = col_cnt > 0
        target = has_corner & ~source
        vert_color = np.zeros((n_verts, 3), dtype=np.float32)
        vert_color[source] = (col_sum[source]
                              / col_cnt[source, None]).astype(np.float32)
    else:  # 'POINT'
        vert_color = rgba[:, :3].copy()
        source = vert_color.max(axis=1) > threshold
        target = ~source

    if not target.any() or not source.any():
        return 0, int(np.count_nonzero(target))

    edge_v = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get("vertices", edge_v)
    adj = [[] for _ in range(n_verts)]
    for a, b in edge_v.reshape(-1, 2):
        adj[a].append(b)
        adj[b].append(a)

    filled = source.copy()
    queue = deque(np.flatnonzero(source).tolist())
    fixed_mask = np.zeros(n_verts, dtype=bool)
    while queue:
        v = queue.popleft()
        for nb in adj[v]:
            if not filled[nb]:
                filled[nb] = True
                vert_color[nb] = vert_color[v]
                if target[nb]:
                    fixed_mask[nb] = True
                queue.append(nb)

    fixed = int(np.count_nonzero(fixed_mask))
    unreachable = int(np.count_nonzero(target & ~filled))
    if fixed:
        if attr.domain == 'CORNER':
            corner_sel = fixed_mask[loop_v]
            rgba[corner_sel, :3] = vert_color[loop_v[corner_sel]]
        else:
            rgba[fixed_mask, :3] = vert_color[fixed_mask]
        attr.data.foreach_set("color", buf)
    return fixed, unreachable


class HDRENC_props(PropertyGroup):
    source_path: StringProperty(
        name="Source Image",
        description="Path to a .exr or .hdr image",
    )
    encoding: EnumProperty(
        name="Encoding",
        items=[
            ('RGBM', "RGBM PNG",
             "Color in RGB, multiplier in alpha. Gamma range [0, 5], "
             "linear range [0, 34.49]"),
            ('DLDR', "dLDR PNG",
             "Double LDR: gamma range [0, 2] mapped to [0, 1], values "
             "above 2 are clamped"),
        ],
        default='RGBM',
    )
    batch_folder: StringProperty(
        name="Batch Folder",
        description="Folder whose .exr/.hdr files will all be converted",
        subtype='DIR_PATH',
    )
    compression_factor: FloatProperty(
        name="Compression Factor",
        description="Vertex colors are divided by this before FBX export "
                    "and multiplied by it (here or in a Unity shader) to "
                    "restore the HDR range",
        min=2.0,
        max=6.0,
        default=4.0,
    )
    darkness_threshold: FloatProperty(
        name="Darkness Threshold",
        description="Vertex colors with all RGB channels at or below "
                    "this value are treated as buried in the ground and "
                    "repaired by Fix Buried Vertices",
        min=0.0,
        max=1.0,
        soft_max=0.05,
        default=0.003,
        precision=4,
        step=0.1,
    )


class HDRENC_OT_browse(Operator, ImportHelper):
    """Browse for a .exr or .hdr image"""
    bl_idname = "hdrenc.browse"
    bl_label = "Browse HDR Image"

    filter_glob: StringProperty(default="*.exr;*.hdr", options={'HIDDEN'})

    def execute(self, context):
        context.scene.hdr_encode.source_path = self.filepath
        return {'FINISHED'}


class HDRENC_OT_generate(Operator):
    """Encode the selected HDR image and open the result in this editor"""
    bl_idname = "hdrenc.generate"
    bl_label = "Generate PNG"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.hdr_encode.source_path)

    def execute(self, context):
        props = context.scene.hdr_encode
        src_path = bpy.path.abspath(props.source_path)

        if not os.path.isfile(src_path):
            self.report({'ERROR'}, "File not found: %s" % src_path)
            return {'CANCELLED'}
        if os.path.splitext(src_path)[1].lower() not in VALID_EXTS:
            self.report({'ERROR'}, "Source must be a .exr or .hdr file")
            return {'CANCELLED'}

        try:
            img = convert_hdr_file(src_path, props.encoding)
        except RuntimeError as ex:
            self.report({'ERROR'}, "Conversion failed: %s" % ex)
            return {'CANCELLED'}

        space = context.space_data
        if space is not None and space.type == 'IMAGE_EDITOR':
            space.image = img

        self.report({'INFO'}, "Saved %s" % img.filepath_raw)
        return {'FINISHED'}


class HDRENC_OT_batch_convert(Operator):
    """Convert every .exr/.hdr file in the batch folder using the
    encoding selected above (Esc cancels)"""
    bl_idname = "hdrenc.batch_convert"
    bl_label = "Convert Folder"
    bl_options = {'REGISTER'}

    _running = False

    @classmethod
    def poll(cls, context):
        return (not cls._running
                and bool(context.scene.hdr_encode.batch_folder))

    def invoke(self, context, event):
        props = context.scene.hdr_encode
        folder = bpy.path.abspath(props.batch_folder)

        if not os.path.isdir(folder):
            self.report({'ERROR'}, "Folder not found: %s" % folder)
            return {'CANCELLED'}

        files = sorted(
            f for f in os.listdir(folder)
            if f.lower().endswith(VALID_EXTS)
            and os.path.isfile(os.path.join(folder, f))
        )
        if not files:
            self.report({'WARNING'}, "No .exr or .hdr files in %s" % folder)
            return {'CANCELLED'}

        self._folder = folder
        self._encoding = props.encoding
        self._files = files
        self._index = 0
        self._converted = 0
        self._failed = []
        self._done = False

        wm = context.window_manager
        wm.progress_begin(0, len(files))
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        HDRENC_OT_batch_convert._running = True
        self._set_status(context)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        # Script path (bpy.ops.hdrenc.batch_convert()) starts the same
        # modal run; from the UI, invoke() is called directly
        return self.invoke(context, None)

    def modal(self, context, event):
        if event.type == 'ESC':
            self._finish(context, cancelled=True)
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if self._index >= len(self._files):
            self._finish(context)
            return {'FINISHED'}

        fname = self._files[self._index]
        try:
            img = convert_hdr_file(
                os.path.join(self._folder, fname), self._encoding)
            # Batch results live on disk; don't pile up datablocks
            bpy.data.images.remove(img)
            self._converted += 1
        except RuntimeError as ex:
            self._failed.append(fname)
            print("HDR Encode: failed to convert %s: %s" % (fname, ex))

        self._index += 1
        context.window_manager.progress_update(self._index)
        self._set_status(context)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self._finish(context, cancelled=True)

    def _set_status(self, context):
        if self._index < len(self._files):
            context.workspace.status_text_set(
                "HDR Encode: %d / %d  —  %s  (Esc to cancel)"
                % (self._index + 1, len(self._files),
                   self._files[self._index]))

    def _finish(self, context, cancelled=False):
        if self._done:
            return
        self._done = True

        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        wm.progress_end()
        context.workspace.status_text_set(None)
        HDRENC_OT_batch_convert._running = False

        total = len(self._files)
        if cancelled:
            self.report({'WARNING'},
                        "Batch cancelled: %d of %d file(s) converted"
                        % (self._converted, total))
        elif self._failed:
            self.report(
                {'WARNING'},
                "Converted %d of %d files. Failed: %s (see console)"
                % (self._converted, total, ", ".join(self._failed)))
        else:
            self.report({'INFO'},
                        "Converted %d file(s) in %s"
                        % (self._converted, self._folder))


class HDRENC_OT_create_vcol(Operator):
    """Ensure every selected mesh has at least one color attribute
    (Face Corner domain, Color data type). Meshes that already have
    one are left untouched"""
    bl_idname = "hdrenc.create_vcol"
    bl_label = "Create Vertex Color Layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and any(o.type == 'MESH' for o in context.selected_objects))

    def execute(self, context):
        created = 0
        skipped = 0
        for obj, mesh in selected_unique_meshes(context):
            if mesh.color_attributes:
                skipped += 1
                continue
            attr = mesh.color_attributes.new("Color", 'FLOAT_COLOR', 'CORNER')
            mesh.color_attributes.active_color = attr
            created += 1

        if created == 0 and skipped == 0:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        self.report({'INFO'},
                    "Created a vertex color layer on %d mesh(es); "
                    "%d already had one" % (created, skipped))
        return {'FINISHED'}


class HDRENC_OT_compress_vcol(Operator):
    """Divide vertex colors by the compression factor, then convert all
    color attributes to Face Corner domain / Byte Color data type (the
    only format Unity imports from FBX)"""
    bl_idname = "hdrenc.compress_vcol"
    bl_label = "Compress HDR Vertex Color"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and any(o.type == 'MESH' for o in context.selected_objects))

    def execute(self, context):
        factor = context.scene.hdr_encode.compression_factor
        layers = 0
        meshes = 0
        for obj, mesh in selected_unique_meshes(context):
            if not mesh.color_attributes:
                continue
            # Scale while the data is still float so HDR values survive
            for attr in mesh.color_attributes:
                scale_color_attribute(attr, 1.0 / factor)
            layers += convert_color_attribute_type(context, obj, 'BYTE_COLOR')
            mesh.update()
            meshes += 1

        if meshes == 0:
            self.report({'WARNING'},
                        "Selected meshes have no color attributes")
            return {'CANCELLED'}
        self.report({'INFO'},
                    "Compressed %d color layer(s) on %d mesh(es) "
                    "(divided by %g, now Face Corner / Byte Color)"
                    % (layers, meshes, factor))
        return {'FINISHED'}


class HDRENC_OT_decompress_vcol(Operator):
    """Convert all color attributes to Face Corner domain / Color (float)
    data type, then multiply vertex colors by the compression factor to
    restore the HDR range"""
    bl_idname = "hdrenc.decompress_vcol"
    bl_label = "Decompress and Restore HDR"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and any(o.type == 'MESH' for o in context.selected_objects))

    def execute(self, context):
        factor = context.scene.hdr_encode.compression_factor
        layers = 0
        meshes = 0
        for obj, mesh in selected_unique_meshes(context):
            if not mesh.color_attributes:
                continue
            # Convert to float first so the multiply isn't clamped to 1
            layers += convert_color_attribute_type(
                context, obj, 'FLOAT_COLOR')
            for attr in mesh.color_attributes:
                scale_color_attribute(attr, factor)
            mesh.update()
            meshes += 1

        if meshes == 0:
            self.report({'WARNING'},
                        "Selected meshes have no color attributes")
            return {'CANCELLED'}
        self.report({'INFO'},
                    "Restored %d color layer(s) on %d mesh(es) "
                    "(multiplied by %g, now Face Corner / Color)"
                    % (layers, meshes, factor))
        return {'FINISHED'}


class HDRENC_OT_fix_buried_vcol(Operator):
    """Fixes the dark shadow bleed caused by vertices that were buried slightly in the ground or inside other objects during the light bake"""
    bl_idname = "hdrenc.fix_buried_vcol"
    bl_label = "Fix Buried Vertices"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and any(o.type == 'MESH' for o in context.selected_objects))

    def execute(self, context):
        threshold = context.scene.hdr_encode.darkness_threshold
        fixed = 0
        unreachable = 0
        meshes = 0
        for obj, mesh in selected_unique_meshes(context):
            attr = mesh.color_attributes.active_color
            if attr is None:
                continue
            f, u = fix_buried_vertices(mesh, attr, threshold)
            mesh.update()
            meshes += 1
            fixed += f
            unreachable += u

        if meshes == 0:
            self.report({'WARNING'},
                        "Selected meshes have no color attributes")
            return {'CANCELLED'}
        if fixed == 0 and unreachable == 0:
            self.report({'INFO'},
                        "No buried vertices found on %d mesh(es) "
                        "(threshold %.4f)" % (meshes, threshold))
        elif unreachable:
            self.report({'WARNING'},
                        "Fixed %d buried vertex(es) on %d mesh(es); "
                        "%d left dark (not connected to any vertex "
                        "above the threshold)"
                        % (fixed, meshes, unreachable))
        else:
            self.report({'INFO'},
                        "Fixed %d buried vertex(es) on %d mesh(es)"
                        % (fixed, meshes))
        return {'FINISHED'}


class HDRENC_OT_smooth_vcol(Operator):
    """Run Blender's built-in 'Smooth Vertex Colors' feature in batch mode - on all selected mesh objects. Only the active color attribute of each mesh is affected"""
    bl_idname = "hdrenc.smooth_vcol"
    bl_label = "Smooth Vertex Colors"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and any(o.type == 'MESH' for o in context.selected_objects))

    def execute(self, context):
        view_layer = context.view_layer
        prev_active = view_layer.objects.active
        smoothed = 0
        skipped = 0
        failed = []
        try:
            for obj, mesh in selected_unique_meshes(context):
                if mesh.color_attributes.active_color is None:
                    skipped += 1
                    continue
                view_layer.objects.active = obj
                try:
                    bpy.ops.object.mode_set(mode='VERTEX_PAINT')
                    try:
                        bpy.ops.paint.vertex_color_smooth()
                    finally:
                        bpy.ops.object.mode_set(mode='OBJECT')
                    smoothed += 1
                except RuntimeError as ex:
                    failed.append(obj.name)
                    print("HDR Encoding Tools: could not smooth %s: %s"
                          % (obj.name, ex))
        finally:
            view_layer.objects.active = prev_active

        if smoothed == 0 and not failed:
            self.report({'WARNING'},
                        "Selected meshes have no color attributes")
            return {'CANCELLED'}
        if failed:
            self.report({'WARNING'},
                        "Smoothed %d mesh(es); failed on: %s (see console)"
                        % (smoothed, ", ".join(failed)))
        else:
            self.report({'INFO'},
                        "Smoothed vertex colors on %d mesh(es)" % smoothed)
        return {'FINISHED'}


class HDRENC_PT_panel(Panel):
    bl_idname = "HDRENC_PT_panel"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "HDR Encoding"
    bl_label = "HDR Encoding Tools"

    def draw(self, context):
        layout = self.layout
        props = context.scene.hdr_encode

        col = layout.column(align=True)
        col.label(text="Source (.exr / .hdr):")
        row = col.row(align=True)
        row.prop(props, "source_path", text="")
        row.operator(HDRENC_OT_browse.bl_idname, text="", icon='FILEBROWSER')

        layout.prop(props, "encoding", text="")
        layout.operator(HDRENC_OT_generate.bl_idname, icon='IMAGE_DATA')


class HDRENC_PT_batch(Panel):
    bl_idname = "HDRENC_PT_batch"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "HDR Encoding"
    bl_parent_id = "HDRENC_PT_panel"
    bl_label = "Batch"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props = context.scene.hdr_encode

        col = layout.column(align=True)
        col.label(text="Batch folder:")
        col.prop(props, "batch_folder", text="")

        layout.operator(HDRENC_OT_batch_convert.bl_idname, icon='FILE_FOLDER')


class HDRENC_PT_vertex_colors(Panel):
    bl_idname = "HDRENC_PT_vertex_colors"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "HDR Encoding"
    bl_parent_id = "HDRENC_PT_panel"
    bl_label = "Vertex Colors"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props = context.scene.hdr_encode

        layout.operator(HDRENC_OT_create_vcol.bl_idname, icon='ADD')

        col = layout.column(align=True)
        col.operator(HDRENC_OT_compress_vcol.bl_idname,
                     icon='FULLSCREEN_EXIT')
        col.operator(HDRENC_OT_decompress_vcol.bl_idname,
                     icon='FULLSCREEN_ENTER')

        layout.prop(props, "compression_factor", slider=True)

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Cleanup:")
        col.prop(props, "darkness_threshold", slider=True)
        col.operator(HDRENC_OT_fix_buried_vcol.bl_idname,
                     icon='SHADING_SOLID')
        col.operator(HDRENC_OT_smooth_vcol.bl_idname, icon='MOD_SMOOTH')


classes = (
    HDRENC_props,
    HDRENC_OT_browse,
    HDRENC_OT_generate,
    HDRENC_OT_batch_convert,
    HDRENC_OT_create_vcol,
    HDRENC_OT_compress_vcol,
    HDRENC_OT_decompress_vcol,
    HDRENC_OT_fix_buried_vcol,
    HDRENC_OT_smooth_vcol,
    HDRENC_PT_panel,
    HDRENC_PT_batch,
    HDRENC_PT_vertex_colors,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.hdr_encode = PointerProperty(type=HDRENC_props)


def unregister():
    del bpy.types.Scene.hdr_encode
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
