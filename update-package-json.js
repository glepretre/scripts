#!/usr/bin/env node

const fs = require('fs');
const { execSync } = require('child_process');
const path = require('path');

const packagePath = path.resolve(process.cwd(), 'package.json');
const packageJson = JSON.parse(fs.readFileSync(packagePath, 'utf8'));

let installed;
try {
  const npmLsOutput = execSync('npm ls --all --json', { encoding: 'utf-8' });
  installed = JSON.parse(npmLsOutput).dependencies || {};
} catch (error) {
  console.error('❌ Error calling `npm ls`:\n', error.message);
  process.exit(1);
}

function updateDeps(deps, sectionName = '') {
  if (!deps) return;
  for (const [pkg, currentVersion] of Object.entries(deps)) {
    const installedVersion = installed[pkg]?.version;
    if (installedVersion) {
      const prefix = currentVersion.startsWith('^') ? '^' : '';
      deps[pkg] = `${prefix}${installedVersion}`;
    } else {
      console.warn(`⚠️  ${pkg} package is not declared in ${sectionName}.`);
    }
  }
}

updateDeps(packageJson.dependencies, 'dependencies');
updateDeps(packageJson.devDependencies, 'devDependencies');
updateDeps(packageJson.peerDependencies, 'peerDependencies');
updateDeps(packageJson.optionalDependencies, 'optionalDependencies');

fs.writeFileSync(packagePath, JSON.stringify(packageJson, null, 2) + '\n');

console.log('✅ package.json updated');
