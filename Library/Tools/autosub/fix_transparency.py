from PIL import Image, ImageDraw
import numpy as np

def autocrop_and_mask(img_path, out_png_path, out_ico_path):
    img = Image.open(img_path).convert("RGBA")
    
    # Get the background color from top-left pixel
    bg_color = img.getpixel((0, 0))
    
    # Convert image to numpy array for fast processing
    data = np.array(img)
    
    # Find all pixels that are NOT the background color
    # Compare only RGB, ignoring alpha
    diff = np.abs(data[:,:,0:3].astype(int) - np.array(bg_color[0:3]))
    mask_bg = np.sum(diff, axis=-1) > 20  # tolerance for compression artifacts
    
    # Find bounding box
    coords = np.argwhere(mask_bg)
    if len(coords) > 0:
        y_min, x_min = coords.min(axis=0)
        y_max, x_max = coords.max(axis=0)
        
        # Crop the image to the bounding box
        cropped = img.crop((x_min, y_min, x_max, y_max))
    else:
        cropped = img
        
    # Now, the cropped image should theoretically be exclusively the squircle.
    w, h = cropped.size
    
    # Make sure it's square
    if w != h:
        size = max(w, h)
        # Pad it to be square
        squared = Image.new("RGBA", (size, size), (0,0,0,0))
        squared.paste(cropped, ((size-w)//2, (size-h)//2))
        cropped = squared
        w = size

    # Create squircle alpha mask
    mask = Image.new('L', (w, w), 0)
    draw = ImageDraw.Draw(mask)
    r = int(w * 0.225) # Apple squircle radius is ~22.5%
    
    # To avoid white aliasing borders from the crop, shrink the mask by 2 pixels
    draw.rounded_rectangle([(2, 2), (w-2, w-2)], radius=r, fill=255)
    
    # Apply mask
    cropped.putalpha(mask)
    
    # Save
    cropped.save(out_png_path)
    icon_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    cropped.save(out_ico_path, sizes=icon_sizes)
    print("Cropped and masked successfully!")

if __name__ == "__main__":
    autocrop_and_mask("autosub_v9.png", "autosub_v9_alpha.png", "autosub_v9.ico")
