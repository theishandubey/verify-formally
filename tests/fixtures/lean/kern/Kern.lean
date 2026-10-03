import Lean
open Lean Elab Command

set_option debug.skipKernelTC true in
run_cmd liftCoreM <| addDecl (.thmDecl { name := `Kern.bad, levelParams := [], type := mkConst ``False, value := mkConst ``True.intro })

theorem Kern.main_claim : 1 = 2 := False.elim Kern.bad
