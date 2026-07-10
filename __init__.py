bl_info = {
    "name": "HDR to PNG Encoder",
    "author": "Theanine3D",
    "version": (1, 1, 0),
    "blender": (5, 0, 0),
    "location": "UV/Image Editor > Sidebar (N) > HDR Encode",
    "description": "Encode .exr/.hdr images to RGBM or dLDR PNG (Unity lightmap encodings)",
    "category": "UV",
}

import os

import bpy
import numpy as np
from bpy.props import EnumProperty, PointerProperty, StringProperty
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


class HDRENC_PT_panel(Panel):
    bl_idname = "HDRENC_PT_panel"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "HDR Encode"
    bl_label = "RGBM / dLDR Encoder"

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
    bl_category = "HDR Encode"
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


classes = (
    HDRENC_props,
    HDRENC_OT_browse,
    HDRENC_OT_generate,
    HDRENC_OT_batch_convert,
    HDRENC_PT_panel,
    HDRENC_PT_batch,
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
