import { existsSync, realpathSync, statSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";

export function fixturePython(
  root: string,
  environment: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
): string {
  const base = realpathSync(root);
  const relative = platform === "win32" ? "Scripts/python.exe" : "bin/python";
  const names = platform === "win32" ? [".venv"] : [".venv", ".venv-wsl"];
  const selected =
    environment.H3_CONTEXT_E2E_PYTHON ?? join(base, ".venv", relative);
  const prefix = dirname(dirname(selected));
  // IMPORTANT: keep the environment directory inside this checkout. Linux's executable may
  // link to its base Python, but a linked foreign venv would mix another lane's dependencies.
  if (
    !isAbsolute(selected) ||
    !names.some((name) => join(base, name, relative) === selected) ||
    !existsSync(join(prefix, "pyvenv.cfg")) ||
    realpathSync(prefix) !== resolve(prefix) ||
    !existsSync(selected) ||
    !statSync(selected).isFile()
  )
    throw new Error(
      "portable fixtures require project-local Python in .venv or Linux .venv-wsl",
    );
  return selected;
}
