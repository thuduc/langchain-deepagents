import { rmSync } from "node:fs";

export default function removeE2eWorkspace() {
  const e2eRoot = process.env.DEEP_AGENTS_E2E_ROOT;
  if (e2eRoot) rmSync(e2eRoot, { recursive: true, force: true });
}
