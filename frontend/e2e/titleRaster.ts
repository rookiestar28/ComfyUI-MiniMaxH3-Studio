// The qualification page imports the same product decoder, resolver and Canvas implementation.
export {
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
} from "../src/contracts/compositionCodec";
export { resolveCompositionScene } from "../src/runtime/sceneResolver";
export { createVisualCompositor } from "../src/runtime/visualCompositor";
