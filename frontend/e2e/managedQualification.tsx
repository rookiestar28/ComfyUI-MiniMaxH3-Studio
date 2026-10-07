import { createManagedQualificationClient } from "../src/host/managedQualificationActions";

// Hermetic transport-contract harness only; the supported-host row uses the real producer.
const client = createManagedQualificationClient({ fetchApi: fetch });
const selection = {
  workspace_handle: `pw_${"a".repeat(32)}`,
  expected_workspace_revision: 2,
  expected_workspace_fingerprint: `sha256:${"b".repeat(64)}`,
  expected_plan_fingerprint: `sha256:${"c".repeat(64)}`,
};
const prepare = document.querySelector<HTMLButtonElement>("#prepare")!;
const read = document.querySelector<HTMLButtonElement>("#read")!;
const status = document.querySelector<HTMLOutputElement>("#status")!;
let fingerprint: string | undefined;
let count = 0;
async function send(
  action: "prepare_managed_readiness" | "read_managed_readiness",
) {
  prepare.disabled = true;
  read.disabled = true;
  try {
    const result = await client.send(
      `readiness.${++count}`,
      action,
      selection,
      action === "read_managed_readiness" ? fingerprint : undefined,
    );
    fingerprint = result.qualification_fingerprint ?? undefined;
    status.value = result.status;
  } catch {
    fingerprint = undefined;
    status.value = "refused";
  } finally {
    prepare.disabled = false;
    read.disabled = fingerprint === undefined;
  }
}
prepare.addEventListener("click", () => void send("prepare_managed_readiness"));
read.addEventListener("click", () => void send("read_managed_readiness"));
