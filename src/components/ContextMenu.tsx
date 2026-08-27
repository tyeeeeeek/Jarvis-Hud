import { useEffect, useRef } from "react";

interface MenuItem {
  label: string;
  icon: string;
  variant?: "danger" | "restore";
  disabled?: boolean;
  action: () => void;
}

interface ContextMenuProps {
  x: number;
  y: number;
  items: MenuItem[];
  onClose: () => void;
  title: string;
}

export function ContextMenu({ x, y, items, onClose, title }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    };
    window.addEventListener("mousedown", handleClick);
    return () => window.removeEventListener("mousedown", handleClick);
  }, [onClose]);

  return (
    <div ref={ref} className="ctx-menu" style={{ left: x, top: y }}>
      <div className="ctx-menu__title">{title}</div>
      {items.map((item, i) => (
        <button
          key={i}
          className={`ctx-menu__item ${item.variant ? `ctx-menu__item--${item.variant}` : ""} ${item.disabled ? "ctx-menu__item--disabled" : ""}`}
          disabled={item.disabled}
          onClick={() => { if (!item.disabled) { item.action(); onClose(); } }}
        >
          <span className="ctx-menu__icon">{item.icon}</span>
          {item.label}
        </button>
      ))}
    </div>
  );
}
