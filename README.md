# HDR to PNG Encoder

<img width="272" height="135" alt="image" src="https://github.com/user-attachments/assets/ef75ac2c-3730-438a-92b9-2e51e0edec89" />

HDR to PNG Encoder is a Blender 5.x addon that can convert an .HDR or .EXR image to a PNG with RGBM or dLDR encoding, for use in game engines like Unity.

RGBM and dLDR encodings are used by game developers to reduce file size of lightmaps. RGBM will often cut the file size of a HDR lightmap by at least half, and dLDR can cut it down even further (albeit with further lossiness.) You can read more about these encodings in [Unity's documentation](https://docs.unity3d.com/Manual/Lightmaps-TechnicalInformation.html).

Finding this addon useful? Please consider starring it ⭐, or [donating](https://ko-fi.com/theanine3d) 🙂<br>

## How to Use
- Go into the UV/Image Editor and open the right sidebar (ie. press N)
- Click on the "HDR Encoder" tab. Choose your .EXR or .HDR image
- Press "Generate PNG"
- After a moment, your new PNG will appear in the UV/Image Editor automatically.
- No need to save the new image manually - the addon also saves it the same folder as your chosen .EXR/HDR image.

## Installation
1. Press the big green Code button above and choose "Download ZIP"
2. Open Blender Preferences and click on the "Addons" tab
3. Click on the "install" button and select your newly downloaded ZIP
