export function createExtensionSetupLifecycle() {
  let state: "idle" | "starting" | "active" | "disposed" = "idle";

  return {
    setup(install: () => void): boolean {
      if (state !== "idle") return false;
      state = "starting";
      try {
        install();
        if (state === "starting") state = "active";
        return true;
      } catch (error) {
        if (state === "starting") state = "idle";
        throw error;
      }
    },
    dispose(cleanup: () => void): boolean {
      if (state === "disposed") return false;
      state = "disposed";
      cleanup();
      return true;
    },
    getState: () => state,
  };
}
