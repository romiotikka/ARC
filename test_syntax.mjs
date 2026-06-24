// Quick syntax check for import_livestats_games.mjs
import { readFileSync } from 'fs';

try {
  const code = readFileSync('./scripts/import_livestats_games.mjs', 'utf8');
  // If the file can be read and has no obvious syntax issues, it should parse
  new Function(code);
  console.log('✓ Syntax is valid');
  process.exit(0);
} catch (error) {
  console.error('✗ Syntax error:', error.message);
  process.exit(1);
}
