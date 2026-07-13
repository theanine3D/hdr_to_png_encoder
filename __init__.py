bl_info = {
    "name": "HDR Encoding Tools",
    "author": "Theanine3D",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "UV/Image Editor > Sidebar (N) > HDR Encoding (image tools); 3D Viewport > Sidebar (N) > HDR Encoding (vertex color tools)",
    "description": "Tools for encoding / compressing HDR images and vertex colors, for use in game engines",
    "category": "UV",
}

import os
from collections import deque

import bpy
import numpy as np
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       PointerProperty, StringProperty)
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


def burial_state(mesh, attr, threshold):
    """Classify every vertex of a color attribute by burial state.

    Returns (buf, rgba, loop_v, source, target, vert_color), or None if
    the mesh or attribute has no data. source marks vertices with at
    least one corner brighter than the threshold (vert_color holds their
    representative color); target marks buried vertices, whose color
    data is entirely at or below the threshold. loop_v is None for
    Vertex-domain attributes."""
    count = len(attr.data)
    n_verts = len(mesh.vertices)
    if count == 0 or n_verts == 0:
        return None

    buf = np.empty(count * 4, dtype=np.float32)
    attr.data.foreach_get("color", buf)
    rgba = buf.reshape(-1, 4)

    loop_v = None
    if attr.domain == 'CORNER':
        loop_v = np.empty(len(mesh.loops), dtype=np.int64)
        mesh.loops.foreach_get("vertex_index", loop_v)
        corner_bright = rgba[:, :3].max(axis=1) > threshold
        # A vertex is buried only if every one of its corners is dark.
        # A non-buried vertex's replacement color averages only its
        # non-dark corners, so seam vertices don't contribute darkness.
        col_sum = np.zeros((n_verts, 3), dtype=np.float64)
        col_cnt = np.zeros(n_verts, dtype=np.int64)
        np.add.at(col_sum, loop_v[corner_bright], rgba[corner_bright, :3])
        np.add.at(col_cnt, loop_v[corner_bright], 1)
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
    return buf, rgba, loop_v, source, target, vert_color


def vertex_adjacency(mesh):
    """Vertex adjacency lists built from the mesh's edges."""
    adj = [[] for _ in range(len(mesh.vertices))]
    edge_v = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get("vertices", edge_v)
    for a, b in edge_v.reshape(-1, 2):
        adj[a].append(b)
        adj[b].append(a)
    return adj


def fix_buried_vertices(mesh, attr, threshold):
    """Replace the color of every buried vertex — one whose RGB channels
    are all at or below the darkness threshold — with the color of the
    nearest connected non-buried vertex (breadth-first over mesh edges,
    so a whole dark patch is filled from its border inward).
    Works on both Vertex and Face Corner domains; alpha is untouched.
    Returns (fixed, unreachable) vertex counts."""
    state = burial_state(mesh, attr, threshold)
    if state is None:
        return 0, 0
    buf, rgba, loop_v, source, target, vert_color = state

    if not target.any() or not source.any():
        return 0, int(np.count_nonzero(target))

    adj = vertex_adjacency(mesh)
    filled = source.copy()
    queue = deque(np.flatnonzero(source).tolist())
    fixed_mask = np.zeros(len(mesh.vertices), dtype=bool)
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


def find_buried_islands(mesh, attr, threshold):
    """Find pieces of geometry that were completely buried (aka completely darkened by shadows)."""
    n_verts = len(mesh.vertices)
    state = burial_state(mesh, attr, threshold)
    if state is None:
        return np.zeros(n_verts, dtype=bool), 0
    source = state[3]
    target = state[4]
    if not target.any():
        return np.zeros(n_verts, dtype=bool), 0

    adj = vertex_adjacency(mesh)
    filled = source.copy()
    queue = deque(np.flatnonzero(source).tolist())
    while queue:
        v = queue.popleft()
        for nb in adj[v]:
            if not filled[nb]:
                filled[nb] = True
                queue.append(nb)
    mask = ~filled  # islands containing no source vertex at all

    islands = 0
    seen = np.zeros(n_verts, dtype=bool)
    for v0 in np.flatnonzero(mask):
        if seen[v0]:
            continue
        islands += 1
        seen[v0] = True
        queue = deque([v0])
        while queue:
            v = queue.popleft()
            for nb in adj[v]:
                if mask[nb] and not seen[nb]:
                    seen[nb] = True
                    queue.append(nb)
    return mask, islands


def mesh_islands(mesh, adj):
    """Label every vertex with a connected-component (geometry island)
    id. Returns (island_id array, island count)."""
    n_verts = len(mesh.vertices)
    island_id = -np.ones(n_verts, dtype=np.int64)
    n_islands = 0
    for v0 in range(n_verts):
        if island_id[v0] != -1:
            continue
        island_id[v0] = n_islands
        queue = deque([v0])
        while queue:
            v = queue.popleft()
            for nb in adj[v]:
                if island_id[nb] == -1:
                    island_id[nb] = n_islands
                    queue.append(nb)
        n_islands += 1
    return island_id, n_islands


def sample_buried_islands_from_other_islands(mesh, attr, threshold,
                                              min_similarity):
    """For every face on a fully buried geometry island (a connected
    component with zero vertices above the darkness threshold, so
    fix_buried_vertices has no brighter vertex in it to copy from), find
    the nearest face - by face-center distance - on a different,
    non-buried island whose normal points in a similar direction
    (cosine similarity >= min_similarity), and copy that donor face's
    averaged corner color onto every vertex/corner of the buried face.
    A buried face with no donor above the similarity threshold anywhere
    in the mesh is left unchanged. Returns the number of vertices whose
    color was set this way."""
    n_verts = len(mesh.vertices)
    n_polys = len(mesh.polygons)
    if n_verts == 0 or n_polys == 0:
        return 0

    state = burial_state(mesh, attr, threshold)
    if state is None:
        return 0
    buf, rgba, loop_v_state, source, target, vert_color = state
    if not target.any():
        return 0

    if attr.domain == 'CORNER':
        loop_v = loop_v_state
    else:
        loop_v = np.empty(len(mesh.loops), dtype=np.int64)
        mesh.loops.foreach_get("vertex_index", loop_v)

    adj = vertex_adjacency(mesh)
    island_id, n_islands = mesh_islands(mesh, adj)
    island_has_source = np.zeros(n_islands, dtype=bool)
    np.logical_or.at(island_has_source, island_id, source)

    loop_start = np.empty(n_polys, dtype=np.int64)
    mesh.polygons.foreach_get("loop_start", loop_start)
    loop_total = np.empty(n_polys, dtype=np.int64)
    mesh.polygons.foreach_get("loop_total", loop_total)
    poly_island = island_id[loop_v[loop_start]]

    # After fix_buried_vertices, any island with a source vertex has
    # already been fully repaired, so remaining dark faces belong
    # exclusively to zero-source islands - and every other face is a
    # safe, genuinely-lit donor candidate.
    buried_face_mask = ~island_has_source[poly_island]
    donor_face_mask = island_has_source[poly_island]
    if not buried_face_mask.any() or not donor_face_mask.any():
        return 0

    normals = np.empty(n_polys * 3, dtype=np.float32)
    mesh.polygons.foreach_get("normal", normals)
    normals = normals.reshape(-1, 3)
    centers = np.empty(n_polys * 3, dtype=np.float32)
    mesh.polygons.foreach_get("center", centers)
    centers = centers.reshape(-1, 3)

    corner_color = rgba[:, :3] if attr.domain == 'CORNER' \
        else vert_color[loop_v]
    poly_color = (np.add.reduceat(corner_color, loop_start, axis=0)
                 / loop_total[:, None])

    donor_idx = np.flatnonzero(donor_face_mask)
    target_idx = np.flatnonzero(buried_face_mask)
    donor_normals = normals[donor_idx]
    donor_centers = centers[donor_idx]
    donor_colors = poly_color[donor_idx]

    # Nearest-donor search, chunked on both axes so memory stays bounded
    # regardless of mesh size (this is a brute-force O(targets*donors)
    # search - fine for the modest face counts a "buried island" repair
    # pass typically involves, but can get slow on very large scenes
    # with many buried faces).
    best_dist2 = np.full(target_idx.size, np.inf, dtype=np.float64)
    best_donor = np.full(target_idx.size, -1, dtype=np.int64)
    TARGET_CHUNK = 500
    DONOR_CHUNK = 4000
    for ts in range(0, target_idx.size, TARGET_CHUNK):
        te = min(ts + TARGET_CHUNK, target_idx.size)
        t_normals = normals[target_idx[ts:te]]
        t_centers = centers[target_idx[ts:te]]
        chunk_best_dist2 = best_dist2[ts:te]
        chunk_best_donor = best_donor[ts:te]
        for ds in range(0, donor_idx.size, DONOR_CHUNK):
            de = min(ds + DONOR_CHUNK, donor_idx.size)
            sim = t_normals @ donor_normals[ds:de].T          # (T, D)
            diff = t_centers[:, None, :] - donor_centers[None, ds:de, :]
            dist2 = np.einsum('tdj,tdj->td', diff, diff)
            dist2[sim < min_similarity] = np.inf
            local_best = np.argmin(dist2, axis=1)
            local_best_dist2 = dist2[np.arange(dist2.shape[0]), local_best]
            better = local_best_dist2 < chunk_best_dist2
            chunk_best_dist2[better] = local_best_dist2[better]
            chunk_best_donor[better] = ds + local_best[better]
        best_dist2[ts:te] = chunk_best_dist2
        best_donor[ts:te] = chunk_best_donor

    matched = best_donor >= 0
    if not matched.any():
        return 0

    matched_target_polys = target_idx[matched]
    matched_colors = donor_colors[best_donor[matched]]

    updated_verts = set()
    for poly_i, color in zip(matched_target_polys, matched_colors):
        ls = loop_start[poly_i]
        lt = loop_total[poly_i]
        if attr.domain == 'CORNER':
            rgba[ls:ls + lt, :3] = color
        else:
            vs = loop_v[ls:ls + lt]
            rgba[vs, :3] = color
            updated_verts.update(vs.tolist())
    attr.data.foreach_set("color", buf)

    if attr.domain == 'CORNER':
        for poly_i in matched_target_polys:
            ls = loop_start[poly_i]
            lt = loop_total[poly_i]
            updated_verts.update(loop_v[ls:ls + lt].tolist())

    return len(updated_verts)


def select_only_vertices(mesh, vert_mask):
    """Replace the mesh's selection with exactly vert_mask, flushing
    vertex selection to edges and faces."""
    mesh.vertices.foreach_set("select", vert_mask)
    n_edges = len(mesh.edges)
    if n_edges:
        edge_v = np.empty(n_edges * 2, dtype=np.int64)
        mesh.edges.foreach_get("vertices", edge_v)
        edge_sel = vert_mask[edge_v.reshape(-1, 2)].all(axis=1)
        mesh.edges.foreach_set("select", edge_sel)
    n_polys = len(mesh.polygons)
    if n_polys:
        loop_v = np.empty(len(mesh.loops), dtype=np.int64)
        mesh.loops.foreach_get("vertex_index", loop_v)
        loop_start = np.empty(n_polys, dtype=np.int64)
        mesh.polygons.foreach_get("loop_start", loop_start)
        face_sel = np.logical_and.reduceat(vert_mask[loop_v], loop_start)
        mesh.polygons.foreach_set("select", face_sel)
    mesh.update()


def ensure_safe_vertex_paint_tool(context):
    """Guard against a Blender tool-system crash on entering Vertex
    Paint mode: if the workspace's saved Vertex Paint tool references a
    brush_type no longer valid in this Blender version (e.g. a stale
    'Blur' reference left over from an older Blender install), Blender's
    automatic tool restoration throws an unhandled exception that it
    prints to the console (the mode switch itself still succeeds -
    Blender just swallows the error internally). Pointing the tool at
    a known-good brush first avoids tripping over the bad reference.
    Best-effort: if this Blender version's tool API differs, this
    quietly does nothing and mode_set proceeds exactly as before."""
    try:
        tool = context.workspace.tools.from_space_view3d_mode(
            'PAINT_VERTEX', create=True)
        if tool is not None:
            tool.idname = 'builtin_brush.Draw'
    except Exception:
        pass


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
        description="A higher value will lighten a greater surface area of"
        "the mesh. A lower value will only lighten the absolute darkest parts"
        "of the mesh. Default value should work in most cases",
        min=0.0,
        max=1.0,
        soft_max=0.05,
        default=0.003,
        precision=4,
        step=0.1,
    )
    direction_similarity: FloatProperty(
        name="Direction Similarity",
        description="If 'Sample from Other Islands' is enabled, this "
                    "setting is used to determine how similar the "
                    "direction of nearby faces must be in order for"
                    "their color to be copied to any buried vertices",
        min=0.0,
        max=100.0,
        default=99.0,
        subtype='PERCENTAGE',
        precision=1,
    )
    sample_from_other_islands: BoolProperty(
        name="Sample from Other Islands",
        description="When parts of the mesh are completely buried, this "
                    "setting will force the 'Fix Buried Vertices' button "
                    "to sample nearby (unburied) faces and copy their color"
                    "to the completley buried islands"
        default=False,
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
    (Face Corner domain, Color data type), and that one of its color
    attributes is set active. Meshes that already have a layer keep it,
    but get an active one assigned if none was set"""
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
        activated = 0
        for obj, mesh in selected_unique_meshes(context):
            if mesh.color_attributes:
                skipped += 1
                if mesh.color_attributes.active_color is None:
                    mesh.color_attributes.active_color = \
                        mesh.color_attributes[0]
                    activated += 1
                continue
            attr = mesh.color_attributes.new("Color", 'FLOAT_COLOR', 'CORNER')
            mesh.color_attributes.active_color = attr
            created += 1

        if created == 0 and skipped == 0:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        msg = ("Created a vertex color layer on %d mesh(es); "
               "%d already had one" % (created, skipped))
        if activated:
            msg += " (%d needed an active layer assigned)" % activated
        self.report({'INFO'}, msg)
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


class HDRENC_OT_find_buried_islands(Operator):
    """Find and highlight every geometry island that is 100% buried, and completely darkened by shadow as a result"""
    bl_idname = "hdrenc.find_buried_islands"
    bl_label = "Find Buried Islands"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and any(o.type == 'MESH' for o in context.selected_objects))

    def execute(self, context):
        threshold = context.scene.hdr_encode.darkness_threshold
        meshes = 0
        verts_found = 0
        islands_found = 0
        first_hit_obj = None
        hit_objects = []
        for obj, mesh in selected_unique_meshes(context):
            attr = mesh.color_attributes.active_color
            if attr is None:
                continue
            meshes += 1
            mask, islands = find_buried_islands(mesh, attr, threshold)
            select_only_vertices(mesh, mask)
            found = int(np.count_nonzero(mask))
            verts_found += found
            islands_found += islands
            if found:
                hit_objects.append(obj.name)
                if first_hit_obj is None:
                    first_hit_obj = obj

        if meshes == 0:
            self.report({'WARNING'},
                        "Selected meshes have no color attributes")
            return {'CANCELLED'}
        if verts_found == 0:
            self.report({'INFO'},
                        "No fully buried islands found on %d mesh(es) "
                        "(threshold %.4f)" % (meshes, threshold))
            return {'FINISHED'}

        context.view_layer.objects.active = first_hit_obj
        context.tool_settings.mesh_select_mode = (True, False, False)
        try:
            bpy.ops.object.mode_set(mode='EDIT')
        except RuntimeError as ex:
            self.report({'WARNING'},
                        "Islands selected, but could not enter Edit "
                        "Mode: %s" % ex)
            return {'FINISHED'}

        self.report({'INFO'},
                    "Selected %d buried island(s) (%d vertices) on: %s"
                    % (islands_found, verts_found, ", ".join(hit_objects)))
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
        props = context.scene.hdr_encode
        threshold = props.darkness_threshold
        sample_other_islands = props.sample_from_other_islands
        min_similarity = props.direction_similarity / 100.0
        fixed = 0
        unreachable = 0
        sampled = 0
        meshes = 0
        for obj, mesh in selected_unique_meshes(context):
            attr = mesh.color_attributes.active_color
            if attr is None:
                continue
            f, u = fix_buried_vertices(mesh, attr, threshold)
            meshes += 1
            fixed += f
            unreachable += u
            if sample_other_islands and u:
                sampled += sample_buried_islands_from_other_islands(
                    mesh, attr, threshold, min_similarity)
            mesh.update()

        if meshes == 0:
            self.report({'WARNING'},
                        "Selected meshes have no color attributes")
            return {'CANCELLED'}
        remaining = unreachable - sampled
        if fixed == 0 and unreachable == 0:
            self.report({'INFO'},
                        "No buried vertices found on %d mesh(es) "
                        "(threshold %.4f)" % (meshes, threshold))
        elif not sample_other_islands:
            if unreachable:
                self.report({'WARNING'},
                            "Fixed %d buried vertex(es) on %d mesh(es); "
                            "%d left dark (not connected to any vertex "
                            "above the threshold)"
                            % (fixed, meshes, unreachable))
            else:
                self.report({'INFO'},
                            "Fixed %d buried vertex(es) on %d mesh(es)"
                            % (fixed, meshes))
        elif remaining:
            self.report({'WARNING'},
                        "Fixed %d buried vertex(es) and sampled %d more "
                        "from other islands on %d mesh(es); %d still "
                        "left dark (no similar-facing donor found)"
                        % (fixed, sampled, meshes, remaining))
        else:
            self.report({'INFO'},
                        "Fixed %d buried vertex(es) and sampled %d more "
                        "from other islands on %d mesh(es)"
                        % (fixed, sampled, meshes))
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
        ensure_safe_vertex_paint_tool(context)
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
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "HDR Encoding"
    bl_label = "Vertex Colors"

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
        col.operator(HDRENC_OT_find_buried_islands.bl_idname,
                     icon='VIEWZOOM')

        col2 = layout.column(align=True)
        col2.prop(props, "darkness_threshold", slider=True)
        col2.prop(props, "direction_similarity", slider=True)
        col2.prop(props, "sample_from_other_islands")
        col2.operator(HDRENC_OT_fix_buried_vcol.bl_idname,
                      icon='SHADING_SOLID')

        layout.operator(HDRENC_OT_smooth_vcol.bl_idname, icon='MOD_SMOOTH')


classes = (
    HDRENC_props,
    HDRENC_OT_browse,
    HDRENC_OT_generate,
    HDRENC_OT_batch_convert,
    HDRENC_OT_create_vcol,
    HDRENC_OT_compress_vcol,
    HDRENC_OT_decompress_vcol,
    HDRENC_OT_find_buried_islands,
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
