import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { sql } from "@codemirror/lang-sql";
import { Prec } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import CodeMirror from "@uiw/react-codemirror";
import { useEffect, useMemo, useRef } from "react";

// Editor ground/text colors are the exact "editor ground" tokens from the
// design handoff README, not the generic oneDark defaults -- oklch(0.185
// 0.020 262) / oklch(0.855 0.010 262).
const nocturneEditorTheme = EditorView.theme(
  {
    "&": {
      backgroundColor: "oklch(0.185 0.020 262)",
      color: "oklch(0.855 0.010 262)",
      height: "100%",
      fontSize: "13.5px",
    },
    ".cm-content": {
      fontFamily: "var(--font-mono)",
      lineHeight: "1.75",
      padding: "16px",
      caretColor: "oklch(0.855 0.010 262)",
    },
    ".cm-gutters": { display: "none" },
    ".cm-scroller": { overflow: "auto" },
    "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": {
      backgroundColor: "color-mix(in srgb, oklch(0.660 0.098 262) 30%, transparent)",
    },
    "&.cm-focused": { outline: "none" },
  },
  { dark: true },
);

export type EditorLanguage = "sql" | "python" | "markdown";

export function CodeEditor({
  value,
  onChange,
  language,
  onRun,
  height = "260px",
}: {
  value: string;
  onChange: (value: string) => void;
  language: EditorLanguage;
  onRun: () => void;
  height?: string;
}) {
  // Keep a ref so the keymap extension (built once per `language`, not per
  // render) always calls the latest onRun rather than a stale closure over
  // whichever onRun existed the moment the extension was constructed.
  const onRunRef = useRef(onRun);
  useEffect(() => {
    onRunRef.current = onRun;
  }, [onRun]);

  const extensions = useMemo(() => {
    const lang = language === "sql" ? sql() : language === "python" ? python() : markdown();
    // Prec.highest so Mod-Enter runs the submission instead of CodeMirror's
    // default newline-insertion binding.
    const runKeymap = Prec.highest(
      keymap.of([
        {
          key: "Mod-Enter",
          run: () => {
            onRunRef.current();
            return true;
          },
        },
      ]),
    );
    return [lang, nocturneEditorTheme, runKeymap];
  }, [language]);

  return (
    <CodeMirror
      value={value}
      height={height}
      minHeight="180px"
      theme="none"
      extensions={extensions}
      onChange={onChange}
      basicSetup={{ lineNumbers: false, foldGutter: false, highlightActiveLine: false }}
    />
  );
}
