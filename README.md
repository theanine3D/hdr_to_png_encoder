# HDR Encoding Tools

<img width="247" height="374" alt="image" src="https://github.com/user-attachments/assets/d568148e-080b-4483-893d-1d674cd3228f" />

HDR Encoding Tools is a Blender 5.x addon that provides features for preparing baked HDR light for use in game engines like Unity. It can convert an .HDR or .EXR image to a PNG with RGBM or dLDR encoding. It can also compress HDR vertex colors.

RGBM and dLDR encodings are used by game developers to reduce file size of lightmaps. RGBM can cut the file size of a HDR lightmap by at least half, and dLDR can cut it down even further (albeit with further lossiness.) You can read more about these encodings in [Unity's documentation](https://docs.unity3d.com/Manual/Lightmaps-TechnicalInformation.html).

HDR vertex colors allow the use of HDR light with a significantly lower memory footprint. However, Unity normally will clamp vertex colors to the 0.0 - 1.0 range, discarding all values above 1.0.  This addon's Compress feature will divide all light valuess, which then allows their safe export to Unity where they can be re-multiplied back to their intended value via a shader.

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

1. Select your meshes, press **Create Vertex Color Layer**
2. Bake lighting to the color attribute (Cycles: Bake target →
   Active Color Attribute)
3. Press **Compress HDR Vertex Color**
4. Export FBX (Geometry → Vertex Colors enabled)
5. In your Unity or Unreal shader, multiply the vertex color by the same factor
   to restore the HDR intensity

Keep the compressed values if you plan to re-export; use **Decompress
and Restore HDR** when you want to preview or re-bake the true HDR
values in Blender. Alpha is never scaled — only RGB.
