/**
 * Frames del avatar central desde img/ en la raíz del proyecto.
 * Orden natural por nombre (01.png, 02.png, frame_10, …).
 */
const modules = import.meta.glob('../img/*.{png,jpg,jpeg,webp,gif}', {
    eager: true,
    query: '?url',
    import: 'default',
});

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

export const AVATAR_FRAME_URLS = Object.keys(modules)
    .sort((a, b) => collator.compare(a, b))
    .map((key) => modules[key]);

/** Precarga en memoria para evitar parpadeos al cambiar de frame. */
export function preloadAvatarFrames(urls = AVATAR_FRAME_URLS) {
    urls.forEach((url) => {
        const img = new Image();
        img.src = url;
    });
}
