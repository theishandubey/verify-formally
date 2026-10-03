---- MODULE CfgDirDecoy ----
EXTENDS Naturals
VARIABLE x
Init == x = 0
Next == x' = IF x < 20 THEN x + 1 ELSE x
TypeOK == x \in Nat
Inv == x < 10
====
