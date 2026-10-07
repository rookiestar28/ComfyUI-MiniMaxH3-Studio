const H3_SUBGRAPH_ID = "4c314f31-ecda-4b08-ae98-faaba1bf613f";

/**
 * Content-free projection of the ComfyUI 0.33 graph shape supplied for M23-11.
 *
 * The fixture deliberately keeps only public node identity, reachable subgraph
 * placement and the incidental port-presentation members that triggered the
 * live refusal. It contains no prompts, widget values, models, media or paths.
 */
export function observedH3CoreCanvas(): Record<string, unknown> {
  return {
    nodes: [
      {
        id: 92,
        type: "SaveVideo",
        inputs: [
          {
            name: "video",
            type: "VIDEO",
            link: null,
            localized_name: "video",
            label: "video",
            shape: 7,
          },
        ],
      },
      {
        id: 105,
        type: H3_SUBGRAPH_ID,
        subgraph_id: H3_SUBGRAPH_ID,
      },
      {
        id: 134,
        type: "comfyui_h3_context.H3Context.ProductShell",
      },
    ],
    definitions: {
      subgraphs: [
        {
          id: H3_SUBGRAPH_ID,
          nodes: [
            {
              id: 9,
              type: "BasicScheduler",
              inputs: [
                {
                  name: "steps",
                  type: "INT",
                  link: null,
                  localized_name: "steps",
                  widget: { name: "steps" },
                },
              ],
              outputs: [
                {
                  name: "SIGMAS",
                  type: "SIGMAS",
                  links: [],
                  localized_name: "SIGMAS",
                },
              ],
            },
            { id: 13, type: "SamplerCustomAdvanced" },
            { id: 14, type: "VAEDecode" },
            { id: 15, type: "CreateVideo" },
            {
              id: 16,
              type: "MiniMaxH3ImageToVideo",
              inputs: [
                {
                  name: "prompt",
                  type: "STRING",
                  link: null,
                  localized_name: "prompt",
                  shape: 7,
                },
              ],
            },
          ],
        },
      ],
    },
  };
}
