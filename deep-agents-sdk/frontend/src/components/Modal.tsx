import type { PropsWithChildren, ReactNode } from "react";
import { Icon } from "./Icons";

interface ModalProps extends PropsWithChildren {
  open: boolean;
  title: string;
  subtitle?: string;
  onClose?: () => void;
  actions?: ReactNode;
  className?: string;
  blocking?: boolean;
}

export function Modal({ open, title, subtitle, onClose, actions, className = "", blocking = false, children }: ModalProps) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (!blocking && event.target === event.currentTarget) onClose?.();
    }}>
      <section className={`modal ${className}`.trim()} role="dialog" aria-modal="true" aria-label={title}>
        <header className="modal-header">
          <div><h2>{title}</h2>{subtitle ? <p>{subtitle}</p> : null}</div>
          {!blocking && onClose ? <button className="icon-button" onClick={onClose} aria-label="Close"><Icon name="close" /></button> : null}
        </header>
        {children}
        {actions ? <footer className="modal-actions">{actions}</footer> : null}
      </section>
    </div>
  );
}
