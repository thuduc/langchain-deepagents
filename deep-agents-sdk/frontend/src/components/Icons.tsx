import type { SVGProps } from "react";

type IconName =
  | "add" | "close" | "copy" | "check" | "edit" | "folder" | "folderOpen"
  | "menu" | "more" | "moon" | "panel" | "settings" | "trash" | "upload"
  | "download" | "file" | "arrow";

const paths: Record<IconName, React.ReactNode> = {
  add: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
  close: <><path d="M18 6 6 18" /><path d="m6 6 12 12" /></>,
  copy: <><rect x="8" y="8" width="11" height="11" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  edit: <><path d="M12 3H6a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3h12a3 3 0 0 0 3-3v-6" /><path d="M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4Z" /></>,
  folder: <path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5Z" />,
  folderOpen: <><path d="M3 9V7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5V10" /><path d="M4 10h17l-2 8.5a2 2 0 0 1-2 1.5H5.5a2 2 0 0 1-2-2.4Z" /></>,
  menu: <><path d="M4 6h16" /><path d="M4 12h16" /><path d="M4 18h16" /></>,
  more: <><path d="M5 12h.01" /><path d="M12 12h.01" /><path d="M19 12h.01" /></>,
  moon: <path d="M12 3a9 9 0 1 0 9 9 6 6 0 0 1-9-9Z" />,
  panel: <><rect x="4" y="5" width="16" height="14" rx="2" /><path d="M9 5v14" /></>,
  settings: <><path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06a2.05 2.05 0 0 1-2.9 2.9l-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1 1.55V21a2.05 2.05 0 0 1-4.1 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.88.34l-.06.06a2.05 2.05 0 0 1-2.9-2.9l.06-.06a1.7 1.7 0 0 0 .34-1.88 1.7 1.7 0 0 0-1.55-1H3a2.05 2.05 0 0 1 0-4.1h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.88l-.06-.06a2.05 2.05 0 0 1 2.9-2.9l.06.06a1.7 1.7 0 0 0 1.88.34 1.7 1.7 0 0 0 1-1.55V3a2.05 2.05 0 0 1 4.1 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.88-.34l.06-.06a2.05 2.05 0 0 1 2.9 2.9l-.06.06a1.7 1.7 0 0 0-.34 1.88 1.7 1.7 0 0 0 1.55 1H21a2.05 2.05 0 0 1 0 4.1h-.09a1.7 1.7 0 0 0-1.55 1Z" /></>,
  trash: <><path d="M4 7h16" /><path d="M10 11v6" /><path d="M14 11v6" /><path d="M6 7l1 14h10l1-14" /><path d="M9 7V4h6v3" /></>,
  upload: <><path d="M12 3v12" /><path d="m7 8 5-5 5 5" /><path d="M5 15v4h14v-4" /></>,
  download: <><path d="M12 4v11" /><path d="m8 11 4 4 4-4" /><path d="M5 20h14" /></>,
  file: <><path d="M7 3h7l4 4v14H7z" /><path d="M14 3v5h5" /></>,
  arrow: <><path d="M5 12h14" /><path d="m14 7 5 5-5 5" /></>,
};

export function Icon({ name, ...props }: SVGProps<SVGSVGElement> & { name: IconName }) {
  return <svg viewBox="0 0 24 24" aria-hidden="true" {...props}>{paths[name]}</svg>;
}
