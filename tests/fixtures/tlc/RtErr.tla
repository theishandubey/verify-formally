---- MODULE RtErr ----
EXTENDS Naturals
VARIABLE x
Init == x = 0
Next == x' = (x + 1) % 3
Inv == IF x = 2 THEN 1 \div 0 = 0 ELSE TRUE
====
