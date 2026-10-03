---- MODULE ExtendsMain ----
EXTENDS ExtendsBase, Naturals
VARIABLE x
Init == x = 0
Next == x' = IF x < MaxVal THEN x + 1 ELSE x
Inv == x <= MaxVal
====
