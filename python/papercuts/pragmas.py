"""Comment directives that disable a region of source text, as Jasper reads them.

Jasper skips `// <word> translate_off` ... `// <word> translate_on` for exactly
these words (measured 2026-10-26, Jasper 2026.03: either comment form, any
whitespace, case-sensitive). It does NOT skip altera, xilinx, mentor, lint,
verilint, snps, `rtl_synthesis off` or `synthesis_off`, nor the misspelled
`synopsis`. Every reader of the original source has to skip the same regions,
or simulation-only code becomes real code on one side only: an initial block
filling a register file with $random (opencores aemb), the only `default:` of a
full_case state machine (opencores mem_ctrl).
"""

TRANSLATE_OFF_WORDS = ("synopsys", "exemplar", "synthesis", "pragma", "ambit", "cadence")

# yosys-slang's read_slang: its own defaults differ (xilinx on; exemplar, ambit,
# cadence off), so they are switched off and the list above given instead. It
# also predefines SYNTHESIS, which neither Jasper nor papercuts' parse does.
READ_SLANG_ARGS = " ".join(["--no-default-translate-off-format", "--no-synthesis-define"]
                           + [f"--translate-off-format {w},translate_off,translate_on"
                              for w in TRANSLATE_OFF_WORDS])


def lexer_options():
    """pyslang LexerOptions that skip the same regions."""
    from pyslang.parsing import LexerOptions
    from pyslang.syntax import CommentHandler

    lo = LexerOptions()
    lo.commentHandlers = {w: {"translate_off": CommentHandler(CommentHandler.Kind.TranslateOff, "translate_on")}
                          for w in TRANSLATE_OFF_WORDS}
    return lo
