---- MODULE ConstFunc ----
EXTENDS Naturals
CONSTANTS N

Fact[n \in 0..3] == IF n = 0 THEN 1 ELSE n * Fact[n-1]
VARIABLE x
Init == x = 0
Next == x < N /\ x' = x + 1
Inv == x <= N
====
