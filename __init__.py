bl_info = {
    "name": "HDR to PNG Encoder",
    "author": "Theanine3D",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "UV/Image Editor > Sidebar (N) > HDR Encode",
    "description": "Encode .EXR/.HDR images to PNG in RGBM or dLDR format",
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
            src = bpy.data.images.load(src_path, check_existing=False)
        except RuntimeError as ex:
            self.report({'ERROR'}, "Could not load image: %s" % ex)
            return {'CANCELLED'}

        try:
            width, height = src.size
            if width == 0 or height == 0:
                self.report({'ERROR'}, "Image has no pixel data")
                return {'CANCELLED'}

            pixels = np.empty(width * height * 4, dtype=np.float32)
            src.pixels.foreach_get(pixels)
            rgb_linear = pixels.reshape(-1, 4)[:, :3]

            out = np.empty((width * height, 4), dtype=np.float32)
            if props.encoding == 'RGBM':
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
        except RuntimeError as ex:
            bpy.data.images.remove(img)
            self.report({'ERROR'}, "Could not save PNG: %s" % ex)
            return {'CANCELLED'}

        space = context.space_data
        if space is not None and space.type == 'IMAGE_EDITOR':
            space.image = img

        self.report({'INFO'}, "Saved %s" % out_path)
        return {'FINISHED'}


class HDRENC_PT_panel(Panel):
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


classes = (
    HDRENC_props,
    HDRENC_OT_browse,
    HDRENC_OT_generate,
    HDRENC_PT_panel,
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
