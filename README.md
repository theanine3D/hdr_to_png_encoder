# HDR Encoding Tools

<img width="242" height="294" alt="image" src="https://github.com/user-attachments/assets/cca0bdf7-d236-4fc5-bbc0-b2bbdca813ad" />

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

- **Fix Buried Vertices** — repairs "shadow bleed": if vertices in a mesh
- are buried even slightly inside other geometry, the light value there bakes
  to black or near-black, and interpolation then smears that darkness up
  the sides of the mesh. This button fixes that by copying the nearest
  non-black color to those vertices that were completely buried.
- **Find Buried Islands** — finds and highlights geometry islands that are
- completely buried - and as a result, completely darkened by shadows
- **Smooth Vertex Colors** — runs Blender's built-in Smooth Vertex
  Colors feature (from Vertex Color Paint mode) in batch mode on every
  selected mesh in one click.
