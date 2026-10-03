---- MODULE Pass ----
EXTENDS Naturals
VARIABLE x

Init == x = 0
Next == IF x < 3 THEN x' = x + 1 ELSE x' = x
Inv == x <= 3

====
