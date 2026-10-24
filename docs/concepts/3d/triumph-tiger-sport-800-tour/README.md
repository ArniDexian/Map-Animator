# Triumph Tiger Sport 800 Tour — approximate 3D model

This is an approximate, scan-like reconstruction from the supplied concept sheet. It is designed to hold a coherent silhouette when rotated, with separate motorcycle and rider geometry, touring luggage, and a denser triangulated surface. It is not intended to match manufacturer CAD.

## Files

- `triumph-tiger-sport-800-tour.glb` — portable glTF 2.0 scene with approximately 84,000 triangles. Import it into Blender, Godot, Unity, Three.js, or another glTF viewer/editor.
- `canonical-views.png` — four cardinal views, elevated 60° above the horizon, on a light background.
- `canonical-views-transparent.png` — the same four views with transparent background.
- `canonical-views-low-angle.png` — the same cardinal views at 25° elevation, which makes the side silhouettes easier to inspect.
- `build_model.py` — standard-library-only generator for the GLB and preview.

## Orientation

- Units are metres.
- Y is up.
- The motorcycle faces +Z.
- The rider and motorcycle are separate scene groups; the main parts are separately named meshes.

The mesh is intentionally approximate and is not manufacturer CAD or a real photogrammetry scan. The sheet gives limited depth information, so hidden surfaces and mechanical details are inferred. Blender is not installed in the current environment, so the deliverable is GLB rather than `.blend`; the GLB can be imported and saved as a Blender project.

Regenerate with:

```sh
python3 build_model.py
```
