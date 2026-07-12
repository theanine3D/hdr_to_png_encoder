# HDR Encoding Tools

<img width="241" height="235" alt="image" src="https://github.com/user-attachments/assets/bf0a787d-088a-4c2e-9a95-9b61777245b8" />

HDR Encoding Tools is a Blender 5.x addon that can prepare baked HDR light for use in game engines like Unity. It can convert an .HDR or .EXR image to a PNG with RGBM or dLDR encoding. It can also compress and clean up HDR vertex colors.

RGBM and dLDR encodings are used by game developers to reduce file size of lightmaps. RGBM can cut the file size of a HDR lightmap by at least half, and dLDR can cut it down even further (albeit with further lossiness.) You can read more about these encodings in [Unity's documentation](https://docs.unity3d.com/Manual/Lightmaps-TechnicalInformation.html).

HDR vertex colors have a significantly lower memory footprint than HDR images. However, Unity normally will clamp vertex colors to the 0.0 - 1.0 range, discarding all values above 1.0.  This addon's Compress feature will divide all light values by a specific compression factor, which then allows their safe export to Unity where they can be re-multiplied back to their intended value via a shader.

Finding this addon useful? Please consider starring it ⭐, or [donating](https://ko-fi.com/theanine3d) 🙂<br>

## Installation
1. Press the big green Code button above and choose "Download ZIP"
2. Open Blender Preferences and click on the "Addons" tab
3. Click on the "install" button and select your newly downloaded ZIP

---

## How to Use (Image-based)
For .HDR and .EXR image encoding:
- Go into the UV/Image Editor and open the right sidebar (ie. press N)
- Click on the "HDR Encoder" tab. Choose your .EXR or .HDR image
- Press "Generate PNG"
- After a moment, your new PNG will appear in the UV/Image Editor automatically.
- No need to save the new image manually - the addon also saves it the same folder as your chosen .EXR/HDR image.

### Encodings

Both PNG encodings store gamma-encoded (1/2.2) values, following Unity's
lightmap conventions:

| Encoding | Gamma range | Linear range | Alpha channel |
|----------|-------------|--------------|---------------|
| RGBM     | [0, 5]      | [0, 34.49]   | Multiplier (M) |
| dLDR     | [0, 2]      | [0, 4.59]    | Unused (1.0)   |

RGBM stores the color divided by its max component; the multiplier that
restores it goes in the alpha channel. dLDR simply maps [0, 2] to [0, 1];
intensities above 2 are clamped.

### Decoding in Unity (Shader Graph, linear color space)

Import the PNG with **sRGB unchecked** (and for RGBM, **Alpha Is
Transparency** unchecked — alpha is a multiplier, not coverage), then:

- **RGBM**: `pow(RGB × A × 5, 2.2)`
- **dLDR**: `pow(RGB × 2, 2.2)`

In a gamma-space project, drop the `pow` and just multiply.

---

## How to Use (Color-based)

This method is the best for filesize / memory savings. The catch: Unity
clamps vertex colors to [0, 1] on FBX import, so HDR values must be
compressed into range first.

Typical workflow:

1.  Open the "HDR Encoding" tab on the righthand sidebar of the 3D Viewport.
2. Select your meshes, press **Create Vertex Color Layer**
3. Bake lighting to the color attribute (Cycles: Bake target →
   Active Color Attribute)
4. Press **Compress HDR Vertex Color**
5. Export FBX (Geometry → Vertex Colors enabled)
6. In your Unity or Unreal shader, multiply the vertex color by the same factor
   to restore the HDR intensity

Keep the compressed values if you plan to re-export; use **Decompress
and Restore HDR** when you want to preview or re-bake the true HDR
values in Blender. Alpha is never scaled — only RGB.

### Cleanup

HDR Encoding Tools also has some cleanup features for light that was baked to
vertex colors. 

- **Fix Buried Vertices** — repairs "shadow bleed": if vertices at the
  base of a mesh poke even slightly below the ground, they bake to
  black or near-black, and interpolation then smears that darkness up
  the sides of the mesh. This button finds every buried vertex — one
  whose RGB channels are all at or below the **Darkness Threshold**
  (default 0.003) — and copies the color of the nearest connected
  non-buried vertex, searching outward through connected vertices until
  it finds one above the threshold. Raise the threshold if your buried
  vertices bake slightly brighter than that; lower it if legitimate
  dark areas are being caught. Works on both Vertex and Face Corner
  domains; alpha is never touched. Vertices in a fully dark
  disconnected island (no bright vertex to reach) are left as-is and
  counted in the report.
- **Find Buried Islands** — selects the geometry that Fix Buried
  Vertices can't repair: whole islands where every vertex is at or
  below the Darkness Threshold, so there's no brighter vertex to copy a
  color from. After marking the islands as selected, it switches to
  Edit Mode automatically so they're immediately highlighted, ready to
  be dealt with manually (moved above ground and re-baked, deleted, or
  painted by hand).
- **Smooth Vertex Colors** — runs Blender's built-in Smooth Vertex
  Colors feature (from Vertex Color Paint mode) in batch mode on every
  selected mesh in one click.
