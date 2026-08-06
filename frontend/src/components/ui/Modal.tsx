import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

interface Props {
  /** Close request: Esc, backdrop click, or the caller's own close button. */
  onClose: () => void;
  children: React.ReactNode;
  /** Tailwind sizing for the panel; defaults to a large centered dialog. */
  panelClassName?: string;
  /** Accessible name for the dialog. */
  ariaLabel?: string;
}

/**
 * Minimal centered modal: dimmed backdrop, Esc-to-close, click-outside-to-
 * close, focus moved into the panel on open and restored on close. Rendered
 * through a portal so ancestor overflow/transform styles can't clip it.
 * Body scroll is locked while open.
 */
export default function Modal({
  onClose,
  children,
  panelClassName = "w-[92vw] h-[88vh] max-w-[1400px]",
  ariaLabel,
}: Props) {
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<Element | null>(null);

  useEffect(() => {
    previousFocus.current = document.activeElement;
    panelRef.current?.focus();
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prevOverflow;
      if (previousFocus.current instanceof HTMLElement) previousFocus.current.focus();
    };
  }, []);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    // Capture phase so Esc closes the modal before any embedded view
    // (e.g. React Flow) swallows the event.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [onClose]);

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ backgroundColor: "rgb(0 0 0 / 0.55)" }}
      onMouseDown={(e) => {
        // Only a click that starts on the backdrop itself closes — dragging
        // out of the panel (e.g. a timeline scrub) must not dismiss it.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        tabIndex={-1}
        className={`flex flex-col min-h-0 rounded-xl border border-border shadow-2xl overflow-hidden outline-none ${panelClassName}`}
        style={{ backgroundColor: "rgb(var(--bg-canvas))" }}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
