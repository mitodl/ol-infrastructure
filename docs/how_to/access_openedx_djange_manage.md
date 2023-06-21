# How To Access An OpenEdX Django Admin manage.py

## Pre-requisites

You will need the following pieces of information to get started:
- The product you want to access e.g. mitxonline, ocw-studio, xpro, mitx
- The environment you want - one of ci, qa, or production.
- Valid credentials to login to the MIT Open Learning AWS Account
  - You should most likely have been given these as a part of your onboarding.
- The MIT oldevops AWS key file - oldevops.pem which at the time of this document's
  writing can be accessed from (TBD: I forget and have asaked my team to remind me.)

## Finding The Right EC2 Instance

1. Log into the AWS console / web UI with your MIT issued credentials.
2. Click the service selector (the tightly grouped bunch of white square boxes in the upper left) and choose EC2.
3. Now click "Instances (running)"
4. You will now see a text box with the prompt "Find instances by attribute..."
5. In this box, type 'edxapp-worker-<product>-<environment>' - for instance, for mitxonline production you would type
   'edxapp-worker-mitxonline-production' and hit enter/return.
6. You should now see a list of instances named edxapp-worker-mitxonline-production
7. You'll need to temporarily add ssh access to the security group for your instance. Right click on the first instance
   in the list and pick Security, then Change Security Groups. Type 'ssh' into the 'add security groups' text box. You
   should see a group appropriate to your product, in the case of mitxonline-production I see mitxonline-production-public-ssh.
   Select that group and click "Add security group" then click the orange Save button at the bottom of the page.
8. Now, left click on the first instance in the list which should expand into instance detail. Click the little square within a
   square Copy icon next to the "Public IPV4 Address".

## Making The Connection

From your laptop, use the oldevops.pem key to ssh to the ubuntu user on the machine whose IP you copied from the previous step.

So for instance:

`ssh -i oldevops.pem ubuntu@34.204.173.109`

At this point you should be good to go and should see a prompt that looks something like:

`ubuntu@ip-10-22-3-162:~$`
