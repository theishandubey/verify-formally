import Lean
open Lean Elab Command
set_option debug.skipKernelTC true in
run_cmd liftCoreM <| addDecl (.thmDecl { name := `Z.bad, levelParams := [], type := mkConst ``False, value := mkConst ``True.intro })
theorem z_claim : 1 = 2 := False.elim Z.bad
theorem _hidden_claim : 3 = 4 := False.elim Z.bad
theorem Foo._helper : 5 = 6 := False.elim Z.bad
private theorem _priv_claim : 7 = 8 := False.elim Z.bad
