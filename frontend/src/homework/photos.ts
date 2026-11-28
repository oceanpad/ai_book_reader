export const maxPhotos = 4;
const targetBytes = 640 * 1024;

export async function encodeWorksheet(file: File): Promise<string> {
  if (file.size > 20 * 1024 * 1024) throw new Error(`${file.name} is too large (maximum 20 MB before resizing).`);
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = url;
    try { await image.decode(); } catch { throw new Error(`Could not open ${file.name}. Choose a JPEG, PNG, or WebP photo.`); }
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    if (!context) throw new Error("This browser could not prepare your photo.");
    const scale = Math.min(1, 2048 / Math.max(image.naturalWidth, image.naturalHeight));
    for (const [size, quality] of [[1, 0.88], [1, 0.75], [0.83, 0.75], [0.67, 0.72]]) {
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale * size));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale * size));
      context.fillStyle = "white";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const data = canvas.toDataURL("image/jpeg", quality);
      const prefix = "data:image/jpeg;base64,";
      if (data.startsWith(prefix) && data.length - prefix.length <= Math.floor(targetBytes / 3) * 4) {
        return data.slice(prefix.length);
      }
    }
    throw new Error(`${file.name} could not be made small enough. Crop it to the questions and try again.`);
  } finally { URL.revokeObjectURL(url); }
}
