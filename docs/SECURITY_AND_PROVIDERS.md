# Security and providers

This page explains what the extension sends, stores and checks. For everyday use, see the
[README](../README.md).

## Contents

- [The default path](#the-default-path)
- [What stays on your host](#what-stays-on-your-host)
- [Opening ComfyUI from another address](#opening-comfyui-from-another-address)
- [Assisted-authoring providers](#assisted-authoring-providers)
- [Media tools (FFmpeg)](#media-tools-ffmpeg)
- [Imported media and rendered videos](#imported-media-and-rendered-videos)
- [Untrusted content](#untrusted-content)
- [What the browser stores](#what-the-browser-stores)
- [What checks do and do not prove](#what-checks-do-and-do-not-prove)
- [Sharing logs and reports](#sharing-logs-and-reports)
- [Licenses](#licenses)

## The default path

Building a prompt uses no provider and no network. It does not open media, resolve URLs, read a
credential store, install packages, start a model or follow instructions found in your material.

Two features add their own boundary, and neither starts by itself:

- **Assisted authoring** runs only after you pick a profile and give the permission it asks for.
  No provider, model or fallback is ever chosen automatically.
- **Media tools** are found on your computer automatically, but downloading them needs you to
  choose **Install**.

## What stays on your host

"Local" here means the ComfyUI host and your browser. If ComfyUI runs on another computer or a
shared server, that server and the people who run it are part of the trust boundary. The sidebar
is not a login system and does not separate people who can reach the same host.

- **Credentials** are supplied while you work and are never written into workflows, reports,
  diagnostics or results.
- **Reports and diagnostics** leave out credentials, provider addresses, signed links, private
  file paths and raw provider responses.
- **Media** stays on its ComfyUI connections. No feature sends media to an assisted-authoring
  provider.
- **Generated videos**: the file ComfyUI saved is left unchanged. After checking that it belongs
  to the run, the workbench keeps a copy in a randomly named folder inside ComfyUI's temporary
  directory and previews from that copy. The browser only receives short-lived handles, never file
  paths. ComfyUI empties that directory when it starts and stops, so the copy lasts at most seven
  days and never beyond the current ComfyUI session. Like ComfyUI's output folder, that directory
  can be served to anyone who can reach the host.

Check exported workflows and screenshots before sharing them. Visible text and media remain
sensitive, and other custom nodes may save their own data.

## Opening ComfyUI from another address

The extension's actions and media previews only answer pages opened at an address ComfyUI itself
serves: `127.0.0.1` or `localhost`, IPv6 loopback, or an IP address ComfyUI listens on. A page at
one address or port cannot act on ComfyUI at another.

For a reverse proxy, a port mapping or a host name other than `localhost`, list the page's exact
origin before starting ComfyUI:

```text
H3_CONTEXT_PUBLIC_ORIGINS=https://comfy.example.test:8443
```

- List one to eight origins, separated by commas, each as `scheme://host[:port]`.
- The proxy must pass the browser's original `Host` header; forwarding headers are ignored.
- If any entry is invalid, the whole setting is ignored and the ComfyUI log says why.
- Refused requests are logged once per kind, without the address.

These checks stop other websites from acting through your browser. They are not a login and do not
protect a host that others can reach. Do not disable them or turn on permissive CORS as a
workaround.

## Assisted-authoring providers

Opening **Settings** does not contact a provider. To use assisted authoring, select a profile and
meet its requirements:

| Choice                      | Network                      | Credential            | What is sent                    |
| --------------------------- | ---------------------------- | --------------------- | ------------------------------- |
| No provider (default)       | none                         | none                  | nothing                         |
| Ollama                      | only the Ollama on your host | none                  | prompt text, to your own Ollama |
| Anthropic, OpenAI or Gemini | HTTPS, after you allow it    | for this session only | prompt text                     |

- Profiles send text only. Choosing a provider never gives it access to media.
- Your permission and credential belong to the exact setup you approved. Changing the provider,
  model or destination clears them.
- Credentials and permissions are kept in the host's memory only. They expire after 30 minutes
  without use or eight hours in total, and a ComfyUI restart discards them. The browser keeps only
  an opaque handle for the tab.
- Each request has fixed limits on calls, time and size. A failure is reported as what it is
  (cancelled, timed out, refused credential, quota, moderation, unsafe setup or connection
  problem); requests are never retried behind your back or sent to another provider.
- Requests go only to the profile's approved destination over HTTPS (or to your own Ollama).
  Redirects are refused and addresses are checked before connecting.
- A listed profile is not a promise that the service is up. Quota, cost, retention and model
  behaviour are set by the provider. Billing is between you and your provider account; read their
  terms before sending private text.

The canvas **H3 Semantic Proposal Producer** has its own local Ollama profile and exact model
checks. Setting up an assisted-authoring profile does not configure it.

The canvas **H3 Official Context-IR** node calls MiniMax's service only when a host integration
supplies the connection (see [host integration](HOST_INTEGRATION.md)). Without one it refuses to run,
so no request is made on your behalf.

## Media tools (FFmpeg)

The editor, Production import, sequence assembly and final rendering use one exact FFmpeg build on
Windows x64 with 64-bit Python (gyan.dev full build `2026-02-26-git-6695528af6`).

- **Only that build is used.** Both programs are checked against fixed SHA-256 digests when found
  and again on every use; any other copy is ignored.
- **Finding it** looks, in order, at an administrator's override, a folder you chose in
  **Settings**, the copy this extension installed, your Python environment and then `PATH` (at most
  32 folders). It never runs a program to identify it and skips network paths and links.
- **Installing** happens only when you choose **Install**. The archive comes over HTTPS from the
  fixed GitHub release, is checked against fixed digests and is unpacked without running anything
  from it. Only the two programs and their license and readme are kept, in ComfyUI's private user
  data. The download sends no prompt, media or credential, never uses a proxy, and never changes
  `PATH` or other packs. On a computer that needs a proxy, choose a folder under **Advanced**
  instead.
- **Remove earlier copy** appears only when an interrupted update left an old copy behind, and runs
  only when you choose it.
- **Running**: FFmpeg runs as a separate process with limits on input size, duration and running
  time. Final rendering also limits memory, CPU time and output size.

## Imported media and rendered videos

- **Importing** Production outputs into the editor adds references to those generated sources. It
  does not upload media, start a generation or place clips on the timeline. Releasing the
  Production project or replacing a source can revoke access; the editor then reports the source as
  unavailable instead of using another take.
- **Previews** (thumbnails, filmstrips and waveforms) are made from those sources and served
  through short-lived handles. They can reveal private content even without a filename. They are
  never sent to assisted-authoring providers.
- **Editor preview media** is held in ComfyUI's memory within a fixed budget, for up to 15 minutes
  after its last use, and is cleared sooner when the source is released or ComfyUI stops.
- **The monitor** composes the preview in your browser. **Final rendering** runs on the host.
- **Rendered videos** are kept for about an hour, at most 16 at a time, in the media tools' private
  temporary folder. **Download original** saves a copy you keep; a downloaded file is outside these
  controls.

## Untrusted content

File paths, URLs, archives, metadata, recognized text, transcripts and provider responses are
treated as untrusted. Media is checked against size, duration, resolution, frame and reference
limits before use. Path tricks, link escapes and unsafe URL schemes are refused.

Text found in media or returned by a provider is information, never an instruction. It cannot
change provider settings, your hard constraints, the workflow or how a run executes. Unknown facts
stay marked as unknown rather than being invented.

## What the browser stores

| Item                        | Where and how long                                   | Contents                                                                                   |
| --------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| App Mode diagnostics        | Local storage, four most recent runs, at most 32 KiB | Status and error codes, timings and fingerprints only; copied only by **Copy diagnostics** |
| Sequence reconnect pointer  | Local storage until it expires                       | An identifier that lets you reconnect to a running whole-video sequence                    |
| Project and session handles | Tab session storage, cleared when the tab closes     | Opaque handles for the current Production project and provider session                     |

The diagnostics never contain prompts, workflows, node titles, media, file names, URLs,
credentials or error text from the host, and are never sent anywhere.

Editor view settings and unsent drafts are kept in the page's memory only. They are not a project
backup: a page reload or host restart does not restore them. The editor's **Saved** status means the
host accepted the edit; it does not create a project file.

## What checks do and do not prove

Prompt validation, guide readiness, media checks and output verification each answer a narrow
question: is the prompt well formed, does the plan cover the official guide, is the media
supported, does the saved file belong to this run. None of them judges visual quality, and none
establishes equivalence with MiniMax's official implementation.

## Sharing logs and reports

When reporting a problem, share diagnostic codes, profile and contract versions, counts and
fingerprints. Remove credentials, cookies, URLs, private paths, prompts, media, personal data and
raw provider responses first.

## Licenses

The project is Apache-2.0; see `LICENSE` and `NOTICE`. The bundled Noto Sans fonts use the SIL Open
Font License 1.1. FFmpeg is not included; it is downloaded only when you choose **Install** and is
licensed separately under GPL-3.0-or-later. MiniMax models, provider services and other external
assets have their own terms. This project is independent and is not affiliated with or endorsed by
MiniMax.
