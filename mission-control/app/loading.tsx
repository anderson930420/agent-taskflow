"use client";

export default function Loading() {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "60vh",
        gap: "16px",
        color: "var(--muted)",
        fontFamily: "inherit",
      }}
    >
      <div
        style={{
          width: "40px",
          height: "40px",
          borderRadius: "999px",
          border: "3px solid var(--border)",
          borderTopColor: "var(--blue)",
          animation: "spin 0.8s linear infinite",
        }}
      />
      <p style={{ fontSize: "0.88rem", margin: 0 }}>
        Loading Mission Control…
      </p>
      <style>{`
        @keyframes spin {
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}