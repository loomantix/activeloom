# Render fidelity fixture — <<FIXTURE_NAME>>

This template exists only for the `render-check` CI job. It pairs with
`expected.md`: apart from replacing placeholder tokens and explicitly collapsing
an emptied placeholder line plus one adjacent separator, the engine must
preserve the remaining template bytes, and the result must be clean under the
pinned prettier.

<<FIXTURE_BODY>>

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

<<FIXTURE_OPTIONAL>><<FIXTURE_OPTIONAL_SECOND>>

---

_Last line of the fixture._
