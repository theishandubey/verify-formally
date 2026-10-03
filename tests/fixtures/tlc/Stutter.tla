---- MODULE Stutter ----
VARIABLE x
Init == x = 0
Next == x' = x
Spec == Init /\ [][Next]_x
Live == <>(x = 1)
====
