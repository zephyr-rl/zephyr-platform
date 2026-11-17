/** Line icons (stroke, 24px grid). Decorative: hidden from screen readers. */
import type { ReactNode } from "react";

function Icon({ children, size = 16 }: { children: ReactNode; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

export const icons = {
  overview: (
    <Icon>
      <rect x="3" y="3" width="7" height="7" />
      <rect x="14" y="3" width="7" height="7" />
      <rect x="3" y="14" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
    </Icon>
  ),
  orders: (
    <Icon>
      <path d="M6 3h12v18l-3-2-3 2-3-2-3 2z" />
      <path d="M9 8h6M9 12h6" />
    </Icon>
  ),
  stock: (
    <Icon>
      <path d="M3 7l9-4 9 4-9 4-9-4z" />
      <path d="M3 7v10l9 4 9-4V7" />
    </Icon>
  ),
  listings: (
    <Icon>
      <path d="M20 12l-8 8-9-9V3h8z" />
      <circle cx="7.5" cy="7.5" r="1.5" />
    </Icon>
  ),
  messages: (
    <Icon>
      <path d="M4 5h16v11H8l-4 4z" />
    </Icon>
  ),
  reviews: (
    <Icon>
      <path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z" />
    </Icon>
  ),
  reports: (
    <Icon>
      <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
    </Icon>
  ),
  automation: (
    <Icon>
      <path d="M13 2L4 14h7l-1 8 9-12h-7z" />
    </Icon>
  ),
  notifications: (
    <Icon>
      <path d="M6 8a6 6 0 0 1 12 0c0 7 3 8 3 8H3s3-1 3-8" />
      <path d="M10 20a2 2 0 0 0 4 0" />
    </Icon>
  ),
  settings: (
    <Icon>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1" />
    </Icon>
  ),
  menu: (
    <Icon size={20}>
      <path d="M4 6h16M4 12h16M4 18h16" />
    </Icon>
  ),
  chevron: (
    <Icon size={14}>
      <path d="M6 9l6 6 6-6" />
    </Icon>
  ),
  alert: (
    <Icon size={13}>
      <path d="M12 4l9 16H3z" />
      <path d="M12 10v4M12 17h.01" />
    </Icon>
  ),
  urgent: (
    <Icon size={13}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v6M12 16.5h.01" />
    </Icon>
  ),
  copy: (
    <Icon size={14}>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V5a1 1 0 0 1 1-1h9" />
    </Icon>
  ),
  key: (
    <Icon size={12}>
      <circle cx="8" cy="15" r="4" />
      <path d="M11 12l9-9M16 7l3 3" />
    </Icon>
  ),
  arrow: (
    <Icon size={14}>
      <path d="M9 6l6 6-6 6" />
    </Icon>
  ),
  logout: (
    <Icon>
      <path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10" />
    </Icon>
  ),
};
