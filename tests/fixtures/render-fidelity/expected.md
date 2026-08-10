# Render fidelity fixture — FidelityFixture

This template exists only for the `render-check` CI job. It pairs with
`expected.md`: apart from replacing placeholder tokens and explicitly collapsing
an emptied placeholder line plus one adjacent separator, the engine must
preserve the remaining template bytes, and the result must be clean under the
pinned prettier.

A populated whole-line placeholder keeps its line and its own spacing.

An opted-in placeholder that renders non-empty keeps its line.

Raw preformatted HTML keeps its blank runs — prettier does not touch them, so
neither may the engine:

<pre>
first preformatted line



last preformatted line
</pre>

An indented code block keeps its blank runs for the same reason:

    first indented line



    last indented line

A fenced block keeps its blank runs too:

```text
first fenced line



last fenced line
```

---

_Last line of the fixture._
