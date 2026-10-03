theorem bad : 1 = 2 := by
  exact absurd rfl (by decide)
