---- MODULE EmptyInit ----
EXTENDS Naturals
VARIABLE x
Init == x \in {}
Next == x' = x + 1
Inv == x < 0
====
