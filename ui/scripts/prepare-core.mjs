// Turns a Higgsfield image-to-3D GLB into the console's hero mesh:
// drop baked textures and materials (the app sets physically based materials in code), weld,
// simplify, centre, normalise to unit height, and compress with meshopt.
//
//   node scripts/prepare-core.mjs <input.glb> public/assets/core.glb [ratio]
import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS, EXTMeshoptCompression } from '@gltf-transform/extensions';
import { center, dedup, meshopt, prune, simplify, weld, getBounds } from '@gltf-transform/functions';
import { MeshoptEncoder, MeshoptSimplifier } from 'meshoptimizer';

const [input, output, ratioArg] = process.argv.slice(2);
if (!input || !output) {
  console.error('usage: node scripts/prepare-core.mjs <input.glb> <output.glb> [ratio]');
  process.exit(2);
}
await MeshoptEncoder.ready;
await MeshoptSimplifier.ready;
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({ 'meshopt.encoder': MeshoptEncoder });
const doc = await io.read(input);
const root = doc.getRoot();

const stats = (label) => {
  let tris = 0;
  for (const mesh of root.listMeshes())
    for (const prim of mesh.listPrimitives()) tris += (prim.getIndices()?.getCount() ?? prim.getAttribute('POSITION').getCount()) / 3;
  console.log(`${label}: meshes=${root.listMeshes().length} triangles=${Math.round(tris)} textures=${root.listTextures().length}`);
};
stats('input');

for (const mesh of root.listMeshes()) {
  for (const prim of mesh.listPrimitives()) {
    for (const semantic of prim.listSemantics()) {
      if (semantic !== 'POSITION' && semantic !== 'NORMAL') prim.setAttribute(semantic, null);
    }
    prim.setMaterial(null);
  }
}
for (const t of root.listTextures()) t.dispose();
for (const m of root.listMaterials()) m.dispose();

await doc.transform(
  dedup(),
  weld(),
  simplify({ simplifier: MeshoptSimplifier, ratio: Number(ratioArg ?? 0.5), error: 0.0008 }),
  center({ pivot: 'center' }),
  prune(),
);

// Normalise to unit height so the scene can size it predictably.
const scene = root.getDefaultScene() ?? root.listScenes()[0];
const b = getBounds(scene);
const height = b.max[1] - b.min[1];
for (const node of scene.listChildren()) node.setScale(node.getScale().map((s) => s / height));
console.log('bounds (before scale):', b.min.map((v) => v.toFixed(3)), b.max.map((v) => v.toFixed(3)));

await doc.transform(meshopt({ encoder: MeshoptEncoder, level: 'medium' }));
doc.createExtension(EXTMeshoptCompression).setRequired(true);
stats('output');
await io.write(output, doc);
