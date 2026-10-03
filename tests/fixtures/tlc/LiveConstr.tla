---- MODULE LiveConstr ----
EXTENDS Naturals
VARIABLE x
Init == x = 0
Next == x' = x + 1
Spec == Init /\ [][Next]_x /\ WF_x(Next)
C == x < 5
P == <>(x = 100)
====
