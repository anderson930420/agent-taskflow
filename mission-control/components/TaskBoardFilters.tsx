"use client";

interface TaskBoardFiltersProps {
  search: string;
  onSearchChange: (value: string) => void;
  showClear?: boolean;
}

export function TaskBoardFilters({
  search,
  onSearchChange,
  showClear = true,
}: TaskBoardFiltersProps) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "10px",
        marginBottom: "14px",
      }}
    >
      {/* Search input */}
      <div style={{ position: "relative", flex: "1 1 280px" }}>
        <span
          style={{
            position: "absolute",
            left: "12px",
            top: "50%",
            transform: "translateY(-50%)",
            color: "var(--muted)",
            fontSize: "0.85rem",
            pointerEvents: "none",
          }}
        >
          🔍
        </span>
        <input
          type="search"
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Search by task key, title, executor, or model…"
          style={{
            width: "100%",
            paddingLeft: "36px",
            paddingRight: showClear && search ? "36px" : "12px",
            height: "38px",
            fontSize: "0.84rem",
            background: "var(--panel)",
            border: "1px solid var(--border)",
            borderRadius: "999px",
            color: "var(--text)",
          }}
        />
        {showClear && search && (
          <button
            onClick={() => onSearchChange("")}
            aria-label="Clear search"
            style={{
              all: "unset",
              cursor: "pointer",
              position: "absolute",
              right: "10px",
              top: "50%",
              transform: "translateY(-50%)",
              color: "var(--muted)",
              fontSize: "0.8rem",
              padding: "2px 6px",
              borderRadius: "999px",
            }}
          >
            ✕
          </button>
        )}
      </div>

      {/* Read-only indicator */}
      <span
        style={{
          fontSize: "0.72rem",
          color: "var(--muted-2)",
          whiteSpace: "nowrap",
          padding: "0 4px",
        }}
      >
        Read-only board
      </span>
    </div>
  );
}