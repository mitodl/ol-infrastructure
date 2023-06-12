# How to Publish an MIT OL Package to NPM

**Note**: This is a VERY rough document. We hate the current
process, but I'm a big believer in not allowing the perfect
to be the enemy of the good :)

## Pre-Requisites

* You will need Vault access for production.
* You'll need to know which Github repository needs publhsing

## Setup

### Git
Check out the Git repository that equates to the NPM module
that needs publishing. Usually the dev asking for help can
give you this if it's not obvious.

### npm login

#### Fun with Vault's Web UI
Now login to npm from the command line. You'll need to have
npm and node installed in order for this to work. If you're
on a Mac, you can use homebrew `brew install npm` but YMMV.

You'll need the username and TOTP code we use for this. Get
from the production [Vault instance](https://vault-production.odl.mit.edu).

Navigate to "platform-secrets" and look for the 'npmjs' entry.

This will get you the username and password. Now you'll need
the TOTP code.

Click the eye icon to reveal the contents of the 'totp-path-mitx-devops'
entry.

Now click the icon to the left of the eye to copy the command you'll
need to run to your local clipboard.

Open the CLI by clicking on the little black box with the '>' in it in the
very upper right of the screen. Now paste the contents if your
clipboard into the bottom section of the screen where you can
enter commands. This will get you the TOTP code you'll need.

#### To the CLI!

To login to npm on the command line and complete setup, run:
`npm login mitx-devops` and hit return.

This will prompt to open a web page. You'll need to supply the
password and TOTP code you got in the previous step. If it's been
too long since you gathered the TOTP code, you may need to hit
up-arrow in the Vault web UI CLI and get a fresh TOTP code.

If you're successful you should see something like:
`Logged in on https://registry.npmjs.org/.`

## Doing The Actual Publishing

Now change directory to the Git repo you checked our earlier and type:

`npm publish`.

If it blows up, check the `packages.json` file and ensure the the
organization is set correctly. It should be 'mitodl'. See
[this commit](https://github.com/mitodl/brand-mitol-residential/commit/8c998e6cc87f4d020b5011be5a8cdf3b003660de) for
an example fix.
