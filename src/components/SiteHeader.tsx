import { useEffect, useRef, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";

const navigation = [
  { to: "/", label: "Latest", end: true },
  { to: "/archive", label: "Archive" },
  { to: "/research-map", label: "Research map" },
  { to: "/artifacts", label: "Artifacts" },
  { to: "/sources", label: "Sources" }
];

export function SiteHeader() {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const location = useLocation();

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        menuButtonRef.current?.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [menuOpen]);

  return (
    <>
      <a className="skip-link" href="#main-content">
        Skip to research report
      </a>
      <header className="site-header">
        <div className="site-header__inner">
          <Link className="wordmark" to="/" aria-label="The Residual, latest pulse">
            <span className="wordmark__signal" aria-hidden="true" />
            <span>The Residual</span>
          </Link>
          <p className="site-header__descriptor">IMF research intelligence</p>
          <button
            ref={menuButtonRef}
            className="menu-toggle"
            type="button"
            aria-expanded={menuOpen}
            aria-controls="primary-navigation"
            onClick={() => setMenuOpen((open) => !open)}
          >
            <span>{menuOpen ? "Close" : "Menu"}</span>
            <span className="menu-toggle__mark" aria-hidden="true" />
          </button>
          <nav
            id="primary-navigation"
            className="primary-navigation"
            data-open={menuOpen ? "true" : "false"}
            aria-label="Primary"
          >
            {navigation.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `primary-navigation__link${isActive ? " is-active" : ""}`
                }
                onClick={() => setMenuOpen(false)}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
    </>
  );
}
