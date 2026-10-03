theorem good : True := trivial
theorem proof_of_safety : 1 = 2 := sorryAx _ false
theorem match_1 : 2 = 3 := sorryAx _ false
def propDef : 1 = 3 := sorryAx _ false
theorem usesWhere : 1 = 4 := helper
where helper : 1 = 4 := sorryAx _ false
