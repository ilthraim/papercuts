"""String literals are re-emitted with their escapes.

The elaborator escaped only backslash and quote, so the newline in
`$display("...\\n")` came out raw and ended the literal: irda, mips_16,
uart16550, vga_lcd and the three usbhostslave designs did not parse in Jasper.
"""

import os
import tempfile

from papercuts.elaborator import elaborate

DESIGN = r'''
module t(input logic clk);
    initial begin
        $display("tab\there \"quoted\" back\\slash\nline two %m");
        $display("bell\007 e\303\251 end");
    end
endmodule
'''


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.sv")
        with open(path, "w") as f:
            f.write(DESIGN)
        out = elaborate([path])
        again = os.path.join(tmp, "again.sv")
        with open(again, "w") as f:
            f.write(out)
        assert elaborate([again]) == out, out
    shown = [l.strip() for l in out.splitlines() if "$display" in l]
    assert shown == [r'$display("tab\there \"quoted\" back\\slash\nline two %m");',
                     r'$display("bell\007 e\303\251 end");'], shown
    return "newline, tab, quote, backslash and non-ASCII bytes escaped; round trip stable"
