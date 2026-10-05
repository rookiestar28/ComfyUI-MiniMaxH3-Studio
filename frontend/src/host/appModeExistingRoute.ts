// App Mode existing-route admission: compatibility of a host-compiled prompt with the
// requested route, the visible graph and the official asset resolution (M23-28 split).

import { isQualifiedExternalReferenceGraph } from "./graphAdapter";
import type { OfficialAssetResolution } from "./officialAssetResolution";
import type { SpliceMediaIds } from "./templateMaterialization";
import {
  existingCompiledProjection,
  listAppModeMediaSources,
} from "./appModeCensus";
import {
  APP_MODE_TASK_MODES,
  auditOverrideNodeType,
  type AppModeCompiledPrompt,
  type AppModeInputs,
  type AppModeMediaKind,
  type AppModeOpaqueSources,
  type AppModeTaskMode,
  type ProductionCanonicalLowering,
  compiledOutputNodes,
  type CompiledPromptRoute,
  compilerNodeType,
  declaredSoundtrackCount,
  type ExistingCompiledAdmission,
  getVideoComponentsNodeType,
  imageGenerationNodeType,
  isBindableDurationSeconds,
  isExternalBoundaryLink,
  isLink,
  isSafeCompiledNodeEnvelope,
  loadAudioNodeType,
  loadImageNodeType,
  loadVideoNodeType,
  MILLISECONDS_PER_SECOND,
  nativeAdapterNodeType,
  planNodeType,
  previewNodeType,
  productShellNodeType,
  record,
  REFERENCE_SOURCE_TYPES,
  referenceGenerationNodeType,
  referenceRegistryNodeType,
  requestNodeType,
  validatorNodeType,
} from "./appModeContract";
import { resolveCompiledDurationSource } from "./appModeTemplateSources";

export function isCompatibleExistingPrompt(
  compiled: AppModeCompiledPrompt,
  route: CompiledPromptRoute = "materialize",
  existingSubject?: ExistingCompiledAdmission,
): boolean {
  const nodes = compiledOutputNodes(compiled);
  if (nodes.length === 0) return false;
  // IMPORTANT: existing source topology is user-owned. This branch must stay
  // ahead of materialize/Connect class counts and Registry parity or valid
  // scaler/custom-producer canvases become impossible to queue again.
  if (route === "existing" && existingSubject !== undefined)
    return existingCompiledProjection(nodes, existingSubject) !== undefined;
  const required: string[] = [
    requestNodeType,
    planNodeType,
    compilerNodeType,
    validatorNodeType,
    nativeAdapterNodeType,
    productShellNodeType,
    previewNodeType,
  ];
  const optional = new Set([
    referenceRegistryNodeType,
    getVideoComponentsNodeType,
  ]);
  const prepared = route === "prepared";
  const generationTypes = new Set<string>([
    imageGenerationNodeType,
    referenceGenerationNodeType,
  ]);
  // M17-20 D11: the prompt is no longer a closed world. A materialized graph is
  // the pinned official template plus this repository's context pipeline, so it
  // carries loaders, a sampler, decode and a sink that are not H3 nodes at all.
  // What stays strict is the part this repository owns: the H3 chain must be
  // complete and singular, and exactly one native anchor must consume the shell's
  // prompt. The repository claims
  // no correctness for the surrounding graph and therefore does not enumerate
  // it.
  const byClass = new Map<string, Array<Record<string, unknown>>>();
  const byId = new Map<string, Record<string, unknown>>();
  const typeById = new Map<string, string>();
  for (const { node } of nodes) {
    const classType = node.class_type;
    const id = nodes.find((candidate) => candidate.node === node)?.id;
    if (typeof classType !== "string" || id === undefined || byId.has(id))
      return false;
    byId.set(id, node);
    typeById.set(id, classType);
    const bucket = byClass.get(classType) ?? [];
    bucket.push(node);
    byClass.set(classType, bucket);
  }
  if ((byClass.get(auditOverrideNodeType)?.length ?? 0) !== (prepared ? 1 : 0))
    return false;
  if (required.some((classType) => byClass.get(classType)?.length !== 1))
    return false;
  const generation = nodes.filter(({ node }) =>
    generationTypes.has(String(node.class_type)),
  );
  if (generation.length !== 1) return false;
  const reference = byClass.get(referenceRegistryNodeType);
  const videoComponents = byClass.get(getVideoComponentsNodeType);
  const isReference =
    generation[0]?.node.class_type === referenceGenerationNodeType;
  if ((reference?.length ?? 0) > 1) return false;
  if (!isReference && videoComponents !== undefined) return false;
  if (isReference && reference?.length !== 1) return false;
  if (isReference && (videoComponents?.length ?? 0) > 3) return false;
  const qualifiedExternalReferenceBoundary =
    isReference && isQualifiedExternalReferenceGraph(compiled.workflow);
  if (qualifiedExternalReferenceBoundary && videoComponents?.length !== 1)
    return false;
  if (
    qualifiedExternalReferenceBoundary &&
    [...REFERENCE_SOURCE_TYPES].some(
      (classType) => (byClass.get(classType)?.length ?? 0) !== 0,
    )
  )
    return false;
  const request = byClass.get(required[0])?.[0];
  const plan = byClass.get(required[1])?.[0];
  const compiler = byClass.get(required[2])?.[0];
  const auditOverride = byClass.get(auditOverrideNodeType)?.[0];
  const validator = byClass.get(required[3])?.[0];
  const adapter = byClass.get(required[4])?.[0];
  const shell = byClass.get(required[5])?.[0];
  const preview = byClass.get(required[6])?.[0];
  if (
    request === undefined ||
    plan === undefined ||
    compiler === undefined ||
    validator === undefined ||
    adapter === undefined ||
    shell === undefined ||
    preview === undefined
  )
    return false;
  const requestInputs = record(request.inputs);
  const planInputs = record(plan.inputs);
  const compilerInputs = record(compiler.inputs);
  const auditOverrideInputs = record(auditOverride?.inputs);
  const validatorInputs = record(validator.inputs);
  const adapterInputs = record(adapter.inputs);
  const shellInputs = record(shell.inputs);
  const previewInputs = record(preview.inputs);
  const generationInputs = record(generation[0].node.inputs);
  const referenceInputs =
    reference?.[0] !== undefined ? record(reference[0].inputs) : undefined;
  const durationSeconds = requestInputs?.duration_seconds;
  const requestedTaskMode = requestInputs?.task_mode;
  // CRITICAL (M23-38 F-13): every frame mode needs the typed declaration that
  // Plan normalizes, including Connect. Connect differs only in where the link
  // comes from: the designated anchor's graph-owned edge, never a caller id.
  const frameRegistryRequired =
    !isReference &&
    (requestedTaskMode === "i2va" ||
      requestedTaskMode === "l2va" ||
      requestedTaskMode === "fl2va");
  if (
    requestInputs === undefined ||
    !(
      (!isReference &&
        requestedTaskMode !== "ref2va" &&
        APP_MODE_TASK_MODES.includes(requestedTaskMode as AppModeTaskMode)) ||
      (requestedTaskMode === "ref2va" && isReference)
    ) ||
    (reference?.length ?? 0) !==
      (isReference || frameRegistryRequired ? 1 : 0) ||
    typeof requestInputs.user_intent !== "string" ||
    requestInputs.user_intent.trim().length === 0 ||
    requestInputs.user_intent.length > 4096 ||
    // M17-25: the authored length. Absent or zero is the host loader sentinel for
    // an unset optional widget, which leaves the node default in force and is a
    // perfectly bindable graph; anything else must be a duration inside the
    // accepted authored range. The producible set is not checked here, because
    // the lattice belongs to the backend alignment authority.
    !isBindableDurationSeconds(durationSeconds) ||
    planInputs === undefined ||
    compilerInputs === undefined ||
    validatorInputs === undefined ||
    adapterInputs === undefined ||
    shellInputs === undefined ||
    previewInputs === undefined ||
    generationInputs === undefined ||
    ((isReference || frameRegistryRequired) && referenceInputs === undefined)
  )
    return false;

  if (
    isReference &&
    !["match", "max"].includes(String(generationInputs.ref_image_size))
  )
    return false;
  const idFor = (classType: string): string | undefined => {
    const classNodes = byClass.get(classType);
    if (classNodes?.length !== 1) return undefined;
    return nodes.find((candidate) => candidate.node === classNodes[0])?.id;
  };
  const exactLink = (
    inputs: Record<string, unknown>,
    name: string,
    classType: string,
    port: number,
  ): boolean => {
    const link = inputs[name];
    const expected = idFor(classType);
    return (
      expected !== undefined &&
      isLink(link) &&
      String(link[0]) === expected &&
      link[1] === port
    );
  };
  if (
    !exactLink(planInputs, "request", required[0], 0) ||
    !exactLink(compilerInputs, "plan", required[1], 0) ||
    !exactLink(validatorInputs, "plan", required[1], 0) ||
    (prepared
      ? auditOverrideInputs === undefined ||
        !exactLink(auditOverrideInputs, "report", required[2], 1) ||
        !exactLink(validatorInputs, "prompt_document", auditOverrideNodeType, 3)
      : !exactLink(validatorInputs, "prompt_document", required[2], 2)) ||
    !exactLink(adapterInputs, "report", required[3], 1) ||
    !exactLink(shellInputs, "report", required[3], 1) ||
    !exactLink(shellInputs, "native_h3_wiring", required[4], 1) ||
    !exactLink(generationInputs, "prompt", required[5], 0) ||
    !exactLink(previewInputs, "report", required[3], 1)
  )
    return false;
  if (
    (isReference || frameRegistryRequired) &&
    !exactLink(planInputs, "reference_registry", referenceRegistryNodeType, 0)
  )
    return false;

  const validTarget = (
    link: unknown,
    allowedTypes: ReadonlySet<string>,
    ports: ReadonlySet<number> = new Set([0]),
  ): link is [string | number, number] =>
    isLink(link) &&
    byId.has(String(link[0])) &&
    allowedTypes.has(typeById.get(String(link[0]) as string) ?? "") &&
    ports.has(link[1]);
  const validLinkList = (
    value: unknown,
    allowedTypes: ReadonlySet<string>,
    ports: ReadonlySet<number> = new Set([0]),
    externalSlots: ReadonlySet<number> = new Set(),
  ): boolean => {
    const links =
      isLink(value) || isExternalBoundaryLink(value, externalSlots)
        ? [value]
        : Array.isArray(value) && value.length > 0
          ? value
          : undefined;
    return (
      links !== undefined &&
      links.every(
        (link) =>
          validTarget(link, allowedTypes, ports) ||
          (qualifiedExternalReferenceBoundary &&
            isExternalBoundaryLink(link, externalSlots)),
      )
    );
  };
  const linkSequence = (value: unknown): unknown[] | undefined => {
    if (
      isLink(value) ||
      (Array.isArray(value) &&
        isExternalBoundaryLink(value, new Set([3, 4, 5, 6])))
    )
      return [value];
    return Array.isArray(value) && value.length > 0 ? value : undefined;
  };
  const sameLinkSequence = (left: unknown, right: unknown): boolean => {
    if (
      Array.isArray(left) &&
      left.length === 0 &&
      Array.isArray(right) &&
      right.length === 0
    )
      return true;
    const leftLinks = linkSequence(left);
    const rightLinks = linkSequence(right);
    return (
      leftLinks !== undefined &&
      rightLinks !== undefined &&
      leftLinks.length === rightLinks.length &&
      leftLinks.every((value, index) => {
        const other = rightLinks[index];
        return (
          Array.isArray(value) &&
          Array.isArray(other) &&
          value.length === 2 &&
          other.length === 2 &&
          String(value[0]) === String(other[0]) &&
          value[1] === other[1]
        );
      })
    );
  };
  const exactExternalSlots = (value: unknown, slots: number[]): boolean => {
    const links = linkSequence(value);
    return (
      links !== undefined &&
      links.length === slots.length &&
      links.every(
        (link, index) =>
          isExternalBoundaryLink(link, new Set(slots)) &&
          Array.isArray(link) &&
          link[1] === slots[index],
      )
    );
  };
  const denseBindings = (
    inputs: Record<string, unknown>,
    aggregateName: string,
    childPattern: RegExp,
    maximum: number,
  ): unknown[] | undefined => {
    const aggregate = inputs[aggregateName];
    if (aggregate !== undefined) return linkSequence(aggregate);
    const indexed: Array<[number, unknown]> = [];
    for (const [name, value] of Object.entries(inputs)) {
      const match = childPattern.exec(name);
      if (match === null) continue;
      const index = Number(match[1]);
      if (!Number.isSafeInteger(index) || index < 0 || index >= maximum)
        return undefined;
      indexed.push([index, value]);
    }
    if (indexed.length === 0) return [];
    indexed.sort((left, right) => left[0] - right[0]);
    if (
      indexed.some(
        ([index, value], position) => index !== position || !isLink(value),
      )
    )
      return undefined;
    return indexed.map(([, value]) => value);
  };
  const imageSources = new Set([loadImageNodeType]);
  const videoSources = new Set([loadVideoNodeType]);
  const audioSources = new Set([loadAudioNodeType]);
  if (!isReference) {
    const taskMode = requestInputs.task_mode as AppModeTaskMode;
    const imageCount = byClass.get(loadImageNodeType)?.length ?? 0;
    const first = generationInputs.first_frame;
    const last = generationInputs.last_frame;
    const validImage = (value: unknown): boolean =>
      validTarget(value, imageSources, new Set([0]));
    const typedFirst = referenceInputs?.first_frame;
    const typedLast = referenceInputs?.last_frame;
    if (route === "connect") {
      // The source branch is foreign and opaque: only the exact link used by
      // both native execution and our typed Registry is owned here. Requiring a
      // LoadImage class/count would inspect unrelated user branches and reject
      // valid custom IMAGE producers.
      if (
        (taskMode === "t2va" &&
          (first !== undefined ||
            last !== undefined ||
            typedFirst !== undefined ||
            typedLast !== undefined)) ||
        (taskMode === "i2va" &&
          (!sameLinkSequence(first, typedFirst) ||
            last !== undefined ||
            typedLast !== undefined)) ||
        (taskMode === "l2va" &&
          (!sameLinkSequence(last, typedLast) ||
            first !== undefined ||
            typedFirst !== undefined)) ||
        (taskMode === "fl2va" &&
          (!sameLinkSequence(first, typedFirst) ||
            !sameLinkSequence(last, typedLast) ||
            (isLink(first) &&
              isLink(last) &&
              String(first[0]) === String(last[0]))))
      )
        return false;
    } else if (
      (taskMode === "t2va" &&
        (imageCount !== 0 ||
          first !== undefined ||
          last !== undefined ||
          typedFirst !== undefined ||
          typedLast !== undefined)) ||
      (taskMode === "i2va" &&
        (imageCount !== 1 ||
          !validImage(first) ||
          last !== undefined ||
          !sameLinkSequence(first, typedFirst) ||
          typedLast !== undefined)) ||
      (taskMode === "l2va" &&
        (imageCount !== 1 ||
          !validImage(last) ||
          first !== undefined ||
          !sameLinkSequence(last, typedLast) ||
          typedFirst !== undefined)) ||
      (taskMode === "fl2va" &&
        (imageCount !== 2 ||
          !validImage(first) ||
          !validImage(last) ||
          !sameLinkSequence(first, typedFirst) ||
          !sameLinkSequence(last, typedLast) ||
          (isLink(first) &&
            isLink(last) &&
            String(first[0]) === String(last[0])))) ||
      (byClass.get(loadVideoNodeType)?.length ?? 0) !== 0 ||
      (byClass.get(loadAudioNodeType)?.length ?? 0) !== 0
    )
      return false;
  }
  if (isReference) {
    const externalImages = new Set([3, 4]);
    const externalVideo = new Set([5]);
    const externalAudio = new Set([6]);
    if (referenceInputs === undefined) return false;
    if (qualifiedExternalReferenceBoundary) {
      if (
        !exactExternalSlots(referenceInputs.images, [3, 4]) ||
        !exactExternalSlots(referenceInputs.videos, [5]) ||
        !exactExternalSlots(referenceInputs.audios, [6]) ||
        !exactExternalSlots(generationInputs.ref_images, [3, 4]) ||
        !exactExternalSlots(generationInputs.ref_audios, [6])
      )
        return false;
      const videoNode = videoComponents?.[0];
      const videoId =
        videoNode === undefined
          ? undefined
          : nodes.find((candidate) => candidate.node === videoNode)?.id;
      const videoInputs = record(videoNode?.inputs);
      if (
        videoId === undefined ||
        videoInputs === undefined ||
        !sameLinkSequence(generationInputs.ref_videos, [videoId, 0]) ||
        !isExternalBoundaryLink(videoInputs.video, externalVideo)
      )
        return false;
    } else {
      const images = denseBindings(
        referenceInputs,
        "images",
        /^images\.image(\d+)$/,
        9,
      );
      const videos = denseBindings(
        referenceInputs,
        "videos",
        /^videos\.video(\d+)$/,
        3,
      );
      const audios = denseBindings(
        referenceInputs,
        "audios",
        /^audios\.audio(\d+)$/,
        3,
      );
      const pairedAudios = denseBindings(
        referenceInputs,
        "paired_audios",
        /^paired_audios\.paired_audio(\d+)$/,
        3,
      );
      const nativeImages = denseBindings(
        generationInputs,
        "ref_images",
        /^ref_images\.ref_image_(\d+)$/,
        9,
      );
      const nativeVideos = denseBindings(
        generationInputs,
        "ref_videos",
        /^ref_videos\.ref_video_(\d+)$/,
        3,
      );
      const nativeVideoAudios = denseBindings(
        generationInputs,
        "ref_video_audios",
        /^ref_video_audios\.ref_video_audio_(\d+)$/,
        3,
      );
      const nativeAudios = denseBindings(
        generationInputs,
        "ref_audios",
        /^ref_audios\.ref_audio_(\d+)$/,
        3,
      );
      if (
        images === undefined ||
        videos === undefined ||
        audios === undefined ||
        nativeImages === undefined ||
        nativeVideos === undefined ||
        nativeVideoAudios === undefined ||
        nativeAudios === undefined ||
        pairedAudios === undefined ||
        images.length + videos.length + audios.length < 1 ||
        images.length > 9 ||
        videos.length > 3 ||
        audios.length > 3 ||
        (images.length > 0 && !validLinkList(images, imageSources)) ||
        (videos.length > 0 && !validLinkList(videos, videoSources)) ||
        (audios.length > 0 && !validLinkList(audios, audioSources)) ||
        !sameLinkSequence(images, nativeImages) ||
        !sameLinkSequence(audios, nativeAudios)
      )
        return false;
      for (const links of [images, videos, audios]) {
        const ids = links.map((link) =>
          Array.isArray(link) ? `${String(link[0])}:${String(link[1])}` : "",
        );
        if (new Set(ids).size !== ids.length) return false;
      }
      const componentIds = (videoComponents ?? []).map(
        (node) => nodes.find((candidate) => candidate.node === node)?.id,
      );
      // M17-17: a reference video may or may not submit its own soundtrack, and
      // both are legitimate canvases. What is never legitimate is the registry
      // and the anchor disagreeing about it: the registry is the owner, so the
      // native soundtrack bindings must be exactly the ones it declares.
      const soundtrackEdges = componentIds.map(
        (id) => [id as string, 1] as [string, number],
      );
      const soundtrackDeclared =
        pairedAudios.length === 0
          ? nativeVideoAudios.length === 0
          : sameLinkSequence(pairedAudios, soundtrackEdges) &&
            sameLinkSequence(nativeVideoAudios, soundtrackEdges);
      const videoBindingsMatch =
        videos.length === 0
          ? componentIds.length === 0 &&
            nativeVideos.length === 0 &&
            nativeVideoAudios.length === 0 &&
            pairedAudios.length === 0
          : componentIds.length === videos.length &&
            // Every video pairs, or none does. A partially paired set is sparse
            // multi-video pairing, which this item does not open.
            (pairedAudios.length === 0 ||
              pairedAudios.length === videos.length) &&
            soundtrackDeclared &&
            sameLinkSequence(
              nativeVideos,
              componentIds.map((id) => [id as string, 0]),
            );
      if (componentIds.some((id) => id === undefined) || !videoBindingsMatch)
        return false;
      for (const [index, component] of (videoComponents ?? []).entries()) {
        const componentInputs = record(component.inputs);
        const videoLink = videos[index];
        if (
          componentInputs === undefined ||
          videoLink === undefined ||
          !sameLinkSequence(componentInputs.video, videoLink)
        )
          return false;
      }
    }
  }

  // Validate every serialized edge, not just its tuple shape. This prevents a
  // foreign node, missing target, or out-of-range port from becoming a queued
  // "existing" graph after a user edits the canvas.
  // IMPORTANT: hard_constraints and intent_graph are structured H3 wire values,
  // not App Mode link sockets. Treating their array-shaped values as links would
  // let a stale compiler inject an extra edge that is absent from the visible
  // canonical graph.
  const recognizedArrayInputs = new Set([
    `${requestNodeType}.duration_seconds`,
    `${planNodeType}.request`,
    `${planNodeType}.reference_registry`,
    `${compilerNodeType}.plan`,
    `${auditOverrideNodeType}.report`,
    `${validatorNodeType}.plan`,
    `${validatorNodeType}.prompt_document`,
    `${nativeAdapterNodeType}.report`,
    `${productShellNodeType}.report`,
    `${productShellNodeType}.native_h3_wiring`,
    `${previewNodeType}.report`,
    `${imageGenerationNodeType}.prompt`,
    `${imageGenerationNodeType}.first_frame`,
    `${imageGenerationNodeType}.last_frame`,
    // The native anchors take the model inputs they encode with. A graph that
    // only conditions is exactly the truncated outcome M17-20 exists to stop
    // shipping, so these are part of the contract rather than foreign wiring --
    // and which node supplies them is the graph's business, not this module's.
    `${imageGenerationNodeType}.clip`,
    `${imageGenerationNodeType}.vae`,
    `${referenceGenerationNodeType}.clip`,
    `${referenceGenerationNodeType}.vae`,
    `${referenceGenerationNodeType}.audio_vae`,
    // The pinned templates drive geometry and length through nodes rather than
    // widgets, so these arrive as links on a materialized graph.
    `${imageGenerationNodeType}.width`,
    `${imageGenerationNodeType}.height`,
    `${imageGenerationNodeType}.length`,
    `${referenceGenerationNodeType}.width`,
    `${referenceGenerationNodeType}.height`,
    `${referenceGenerationNodeType}.length`,
    `${referenceGenerationNodeType}.prompt`,
    `${referenceGenerationNodeType}.ref_images`,
    `${referenceGenerationNodeType}.ref_videos`,
    `${referenceGenerationNodeType}.ref_video_audios`,
    `${referenceGenerationNodeType}.ref_audios`,
    `${referenceRegistryNodeType}.first_frame`,
    `${referenceRegistryNodeType}.last_frame`,
    `${referenceRegistryNodeType}.images`,
    `${referenceRegistryNodeType}.videos`,
    `${referenceRegistryNodeType}.paired_audios`,
    `${referenceRegistryNodeType}.audios`,
    `${getVideoComponentsNodeType}.video`,
  ]);
  const allowedInputNames = new Map<string, ReadonlySet<string>>([
    [
      requestNodeType,
      new Set([
        "task_mode",
        "user_intent",
        "duration_seconds",
        "hard_constraints",
      ]),
    ],
    [planNodeType, new Set(["request", "reference_registry", "intent_graph"])],
    [compilerNodeType, new Set(["plan"])],
    [
      auditOverrideNodeType,
      new Set([
        "report",
        "base_report_fingerprint",
        "revision",
        "reason",
        "prompt_text",
      ]),
    ],
    [validatorNodeType, new Set(["plan", "prompt_document"])],
    [nativeAdapterNodeType, new Set(["report"])],
    [productShellNodeType, new Set(["report", "native_h3_wiring"])],
    [previewNodeType, new Set(["report"])],
    [
      referenceRegistryNodeType,
      // M17-17: `paired_audios` is how the registry is told that a reference
      // video submits its own soundtrack. Leaving it out of this set refused a
      // canvas that declared the pairing the documented way.
      new Set([
        "first_frame",
        "last_frame",
        "images",
        "videos",
        "paired_audios",
        "audios",
      ]),
    ],
    [
      imageGenerationNodeType,
      new Set([
        "clip",
        "vae",
        "prompt",
        "first_frame",
        "last_frame",
        "width",
        "height",
        "length",
      ]),
    ],
    [
      referenceGenerationNodeType,
      new Set([
        "clip",
        "vae",
        "audio_vae",
        "prompt",
        "width",
        "height",
        "length",
        "ref_image_size",
        "ref_images",
        "ref_videos",
        "ref_video_audios",
        "ref_audios",
      ]),
    ],
    [getVideoComponentsNodeType, new Set(["video"])],
    [loadImageNodeType, new Set(["image"])],
    [loadVideoNodeType, new Set(["file"])],
    [loadAudioNodeType, new Set(["audio"])],
  ]);
  const isDynamicReferenceInput = (classType: unknown, name: string): boolean =>
    (classType === referenceRegistryNodeType &&
      /^(?:images\.image|videos\.video|paired_audios\.paired_audio|audios\.audio)\d+$/.test(
        name,
      )) ||
    (classType === referenceGenerationNodeType &&
      /^(?:ref_images\.ref_image_|ref_videos\.ref_video_|ref_video_audios\.ref_video_audio_|ref_audios\.ref_audio_)\d+$/.test(
        name,
      ));
  for (const node of nodes) {
    const inputs = record(node.node.inputs);
    if (inputs === undefined) return false;
    const classType = node.node.class_type;
    const allowedNames = allowedInputNames.get(String(classType));
    // M17-20 D11: a materialized graph carries the template's loaders, sampler,
    // decode and sink. Their input names belong to the host's node definitions,
    // not to this contract, so they are not enumerated here -- an allowlist that
    // has to grow every time an official template gains a node would fail closed
    // on correct graphs, which is worse than not claiming what it cannot check.
    // The H3 chain and the media nodes this repository wires stay exhaustive.
    if (allowedNames === undefined) {
      // One property still holds for a foreign node: a link has to point at a
      // node that is in this prompt. The host reads a bare two-element array as
      // `[origin, slot]` and would fail on a missing origin, so a dangling edge
      // is a malformed prompt no matter whose node carries it.
      for (const value of Object.values(inputs)) {
        if (isLink(value) && !byId.has(String(value[0]))) return false;
        if (Array.isArray(value) && !isLink(value))
          for (const entry of value)
            if (isLink(entry) && !byId.has(String(entry[0]))) return false;
      }
      continue;
    }
    if (
      Object.keys(inputs).some(
        (name) =>
          !allowedNames.has(name) && !isDynamicReferenceInput(classType, name),
      )
    )
      return false;
    for (const [name, value] of Object.entries(inputs)) {
      if (Array.isArray(value)) {
        if (
          !recognizedArrayInputs.has(`${classType}.${name}`) &&
          !isDynamicReferenceInput(classType, name)
        )
          return false;
        const allowMany =
          (classType === "comfyui_h3_context.H3Context.ReferenceRegistry" &&
            ["images", "videos", "audios"].includes(name)) ||
          (classType === "MiniMaxH3ReferenceToVideo" &&
            [
              "ref_images",
              "ref_videos",
              "ref_video_audios",
              "ref_audios",
            ].includes(name));
        const externalSlots =
          qualifiedExternalReferenceBoundary &&
          classType === "comfyui_h3_context.H3Context.ReferenceRegistry" &&
          name === "images"
            ? new Set([3, 4])
            : qualifiedExternalReferenceBoundary &&
                classType ===
                  "comfyui_h3_context.H3Context.ReferenceRegistry" &&
                name === "videos"
              ? new Set([5])
              : qualifiedExternalReferenceBoundary &&
                  classType ===
                    "comfyui_h3_context.H3Context.ReferenceRegistry" &&
                  name === "audios"
                ? new Set([6])
                : qualifiedExternalReferenceBoundary &&
                    classType === "MiniMaxH3ReferenceToVideo" &&
                    name === "ref_images"
                  ? new Set([3, 4])
                  : qualifiedExternalReferenceBoundary &&
                      classType === "MiniMaxH3ReferenceToVideo" &&
                      name === "ref_audios"
                    ? new Set([6])
                    : qualifiedExternalReferenceBoundary &&
                        classType === "GetVideoComponents" &&
                        name === "video"
                      ? new Set([5])
                      : new Set<number>();
        const linkIsKnown = (link: unknown): boolean =>
          (isLink(link) && byId.has(String(link[0]))) ||
          isExternalBoundaryLink(link, externalSlots);
        const singleLink =
          isLink(value) || isExternalBoundaryLink(value, externalSlots);
        if (
          !singleLink &&
          !(
            allowMany &&
            value.length > 0 &&
            value.every((link) => linkIsKnown(link))
          )
        )
          return false;
        if (singleLink && !linkIsKnown(value)) return false;
        if (
          allowMany &&
          !singleLink &&
          value.every((link) => linkIsKnown(link)) === false
        )
          return false;
      }
    }
  }
  return true;
}

export function resolveOpaqueSources(
  serialized: unknown,
  compiled: AppModeCompiledPrompt,
  inputs: AppModeInputs,
): AppModeOpaqueSources | undefined {
  if (inputs.task_mode === "t2va") return {};
  const visible = new Map(
    listAppModeMediaSources(serialized).map((source) => [
      `${source.kind}:${source.node_id}`,
      source,
    ]),
  );
  const compiledById = new Map(
    compiledOutputNodes(compiled).map(({ id, node }) => [id, node] as const),
  );
  const resolve = (
    id: string | undefined,
    kind: AppModeMediaKind = "image",
  ): Record<string, unknown> | undefined => {
    if (id === undefined || !visible.has(`${kind}:${id}`)) return undefined;
    const node = compiledById.get(id);
    const expectedType =
      kind === "image"
        ? loadImageNodeType
        : kind === "video"
          ? loadVideoNodeType
          : loadAudioNodeType;
    return node?.class_type === expectedType && isSafeCompiledNodeEnvelope(node)
      ? node
      : undefined;
  };
  const resolveMany = (
    ids: readonly string[] | undefined,
    kind: AppModeMediaKind,
  ): Record<string, unknown>[] | undefined => {
    const resolved = (ids ?? []).map((id) => resolve(id, kind));
    return resolved.every(
      (source): source is Record<string, unknown> => source !== undefined,
    )
      ? resolved
      : undefined;
  };
  if (inputs.task_mode === "ref2va") {
    const images = resolveMany(inputs.reference_image_sources, "image");
    const videos = resolveMany(inputs.reference_video_sources, "video");
    const audios = resolveMany(inputs.reference_audio_sources, "audio");
    if (images === undefined || videos === undefined || audios === undefined)
      return undefined;
    return {
      reference_images: images,
      reference_videos: videos,
      reference_audios: audios,
    };
  }
  const first = resolve(inputs.first_frame_source);
  const last = resolve(inputs.last_frame_source);
  if (
    (inputs.task_mode === "i2va" && first !== undefined) ||
    (inputs.task_mode === "l2va" && last !== undefined) ||
    (inputs.task_mode === "fl2va" && first !== undefined && last !== undefined)
  ) {
    const resolved: AppModeOpaqueSources = {};
    if (first !== undefined) resolved.first_frame = first;
    if (last !== undefined) resolved.last_frame = last;
    return resolved;
  }
  return undefined;
}

export function compiledPromptMatchesRequestedRoute(
  compiled: AppModeCompiledPrompt,
  inputs: AppModeInputs,
  route: CompiledPromptRoute,
  materialized?: SpliceMediaIds,
  existingSubject?: ExistingCompiledAdmission,
  canonicalLowering?: ProductionCanonicalLowering,
): boolean {
  const useExisting = route === "existing";
  const nodes = compiledOutputNodes(compiled);
  if (useExisting && existingSubject !== undefined) {
    const projection = existingCompiledProjection(nodes, existingSubject);
    const requestInputs = record(projection?.request.inputs);
    const durationInputs = record(projection?.durationSource.inputs);
    return (
      projection !== undefined &&
      requestInputs?.user_intent === inputs.user_intent &&
      durationInputs?.value ===
        inputs.duration_milliseconds / MILLISECONDS_PER_SECOND
    );
  }
  const request = nodes.find(
    ({ node }) => node.class_type === requestNodeType,
  )?.node;
  const generation = nodes.find(
    ({ node }) =>
      node.class_type ===
      (inputs.task_mode === "ref2va"
        ? referenceGenerationNodeType
        : imageGenerationNodeType),
  )?.node;
  const requestInputs = record(request?.inputs);
  const generationInputs = record(generation?.inputs);
  const requestedSeconds =
    inputs.duration_milliseconds / MILLISECONDS_PER_SECOND;
  if (
    requestInputs?.task_mode !== inputs.task_mode ||
    requestInputs.user_intent !== inputs.user_intent ||
    generationInputs === undefined
  )
    return false;
  if (route === "prepared") {
    const audit = nodes.find(
      ({ node }) => node.class_type === auditOverrideNodeType,
    )?.node;
    const auditInputs = record(audit?.inputs);
    if (
      canonicalLowering === undefined ||
      auditInputs?.base_report_fingerprint !==
        canonicalLowering.baseReportFingerprint ||
      auditInputs.revision !== canonicalLowering.overrideRevision ||
      auditInputs.reason !== canonicalLowering.reason ||
      auditInputs.prompt_text !== canonicalLowering.canonicalPrompt
    )
      return false;
  }
  const byId = new Map(nodes.map(({ id, node }) => [id, node] as const));
  const requestDuration = requestInputs.duration_seconds;
  const requestDurationSource = resolveCompiledDurationSource(
    byId,
    requestDuration,
  );
  const requestDurationValue = isLink(requestDuration)
    ? requestDurationSource?.value
    : requestDuration;
  if (requestDurationValue !== requestedSeconds) return false;
  if (
    (route === "materialize" || route === "prepared") &&
    !isLink(requestDuration)
  )
    return false;
  const sourceId = (value: unknown): string | undefined =>
    isLink(value) ? String(value[0]) : undefined;
  const sameExactLink = (left: unknown, right: unknown): boolean =>
    isLink(left) &&
    isLink(right) &&
    String(left[0]) === String(right[0]) &&
    left[1] === right[1];
  const linkList = (value: unknown): [string | number, number][] =>
    isLink(value)
      ? [value]
      : Array.isArray(value) && value.every(isLink)
        ? value
        : [];
  if (inputs.task_mode === "ref2va") {
    // On the connect route the reference set belongs to the graph the user
    // assembled, not to a sidebar selection, so there is no count to match
    // against. What was verified is that the designated anchor is the reference
    // anchor and that its task mode is the one asked for; the rest of its wiring
    // is theirs.
    if (route === "connect") return true;
    const expectedImages = inputs.reference_image_sources?.length ?? 0;
    const expectedVideos = inputs.reference_video_sources?.length ?? 0;
    const expectedAudios = inputs.reference_audio_sources?.length ?? 0;
    const expectedSoundtracks = declaredSoundtrackCount(inputs);
    const registryInputs = record(
      nodes.find(({ node }) => node.class_type === referenceRegistryNodeType)
        ?.node.inputs,
    );
    const countBindings = (aggregate: string, pattern: RegExp): number => {
      if (generationInputs[aggregate] !== undefined)
        return linkList(generationInputs[aggregate]).length;
      return Object.keys(generationInputs).filter((name) => pattern.test(name))
        .length;
    };
    const countRegistry = (aggregate: string, pattern: RegExp): number => {
      if (registryInputs === undefined) return -1;
      if (registryInputs[aggregate] !== undefined)
        return linkList(registryInputs[aggregate]).length;
      return Object.keys(registryInputs).filter((name) => pattern.test(name))
        .length;
    };
    return (
      countBindings("ref_images", /^ref_images\.ref_image_\d+$/) ===
        expectedImages &&
      countBindings("ref_videos", /^ref_videos\.ref_video_\d+$/) ===
        expectedVideos &&
      // M17-17: the soundtrack count is read from the state the user declared,
      // not assumed from the video count, and the registry that owns the pairing
      // has to say the same thing the anchor does. A graph where only one of them
      // carries the soundtrack is the drift this item removed.
      countBindings(
        "ref_video_audios",
        /^ref_video_audios\.ref_video_audio_\d+$/,
      ) === expectedSoundtracks &&
      countRegistry("paired_audios", /^paired_audios\.paired_audio\d+$/) ===
        expectedSoundtracks &&
      countBindings("ref_audios", /^ref_audios\.ref_audio_\d+$/) ===
        expectedAudios
    );
  }
  const first = sourceId(generationInputs.first_frame);
  const last = sourceId(generationInputs.last_frame);
  // The materialized route no longer knows the loader ids in advance: they come
  // from the template, which owns them, and the splice reports which node it
  // bound to each role. Asserting fixed ids here was only ever true of a graph
  // this module built itself.
  const identity = (value: number | undefined): string | undefined =>
    value === undefined ? undefined : String(value);
  const registryInputs = record(
    nodes.find(({ node }) => node.class_type === referenceRegistryNodeType)
      ?.node.inputs,
  );
  // Connect derives roles from the designated anchor, then creates the typed
  // Plan declaration from those exact links. We claim no source class or branch
  // semantics, but Registry/native link parity is repository-owned and is what
  // prevents a queued missing_first_frame failure.
  if (route === "connect") {
    const typedFirst = registryInputs?.first_frame;
    const typedLast = registryInputs?.last_frame;
    return (
      (inputs.task_mode === "t2va" &&
        first === undefined &&
        last === undefined &&
        registryInputs === undefined) ||
      (inputs.task_mode === "i2va" &&
        first !== undefined &&
        last === undefined &&
        sameExactLink(generationInputs.first_frame, typedFirst) &&
        typedLast === undefined) ||
      (inputs.task_mode === "l2va" &&
        last !== undefined &&
        first === undefined &&
        sameExactLink(generationInputs.last_frame, typedLast) &&
        typedFirst === undefined) ||
      (inputs.task_mode === "fl2va" &&
        first !== undefined &&
        last !== undefined &&
        first !== last &&
        sameExactLink(generationInputs.first_frame, typedFirst) &&
        sameExactLink(generationInputs.last_frame, typedLast))
    );
  }
  const expectedFirst = useExisting
    ? inputs.first_frame_source
    : identity(materialized?.firstFrame);
  const expectedLast = useExisting
    ? inputs.last_frame_source
    : identity(materialized?.lastFrame);
  const typedFirst = registryInputs?.first_frame;
  const typedLast = registryInputs?.last_frame;
  return (
    (inputs.task_mode === "t2va" &&
      first === undefined &&
      last === undefined &&
      registryInputs === undefined) ||
    (inputs.task_mode === "i2va" &&
      first === expectedFirst &&
      last === undefined &&
      sameExactLink(generationInputs.first_frame, typedFirst) &&
      typedLast === undefined) ||
    (inputs.task_mode === "l2va" &&
      last === expectedLast &&
      first === undefined &&
      sameExactLink(generationInputs.last_frame, typedLast) &&
      typedFirst === undefined) ||
    (inputs.task_mode === "fl2va" &&
      first === expectedFirst &&
      last === expectedLast &&
      first !== last &&
      sameExactLink(generationInputs.first_frame, typedFirst) &&
      sameExactLink(generationInputs.last_frame, typedLast))
  );
}

/** Prove that host compilation retained every exact materialized COMBO value. */
export function compiledPromptMatchesOfficialAssetResolution(
  compiled: AppModeCompiledPrompt,
  resolution: OfficialAssetResolution,
): boolean {
  if (resolution.unresolvedSlots.length > 0) return false;
  const nodes = compiledOutputNodes(compiled);
  return resolution.bindings.every((binding) => {
    const matches = nodes.filter(({ node }) => {
      const inputs = record(node.inputs);
      return (
        node.class_type === binding.loaderType &&
        inputs?.[binding.widgetName] === binding.value
      );
    });
    return matches.length === 1;
  });
}
