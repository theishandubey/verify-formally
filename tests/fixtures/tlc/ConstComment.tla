---- MODULE ConstComment ----
EXTENDS Naturals

CONSTANTS
  A,   \* first
  \* a comment line
  B

VARIABLE x

Init == x = 0
Next == x' = (x + 1) % 3
Inv == x < 5
====
