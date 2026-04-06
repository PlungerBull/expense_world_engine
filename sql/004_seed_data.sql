-- 004: Seed global_currencies with Phase 1 currencies
INSERT INTO global_currencies (code, name, symbol) VALUES
    ('USD', 'US Dollar', '$'),
    ('PEN', 'Peruvian Sol', 'S/')
ON CONFLICT (code) DO NOTHING;
