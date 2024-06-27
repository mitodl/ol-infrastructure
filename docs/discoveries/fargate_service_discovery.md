# Integrating Fargate Services Into Our Existing Systems Architecture

## The Goal
We want to start using ECS Fargate from AWS as a low-complexity way of running
containerized workloads. It allows us to specify a set of containers that we would like
to run as a single task, with simple autoscaling and resource allocation, and no need to
manage the underlying servers. At face value this is a great option, even with the
slight cost markup that it brings. It is highly likely that we will save at least that
much money in engineering time.

## The Challenges
In order for us to properly integrate applications and services into our infrastructure,
we need to be able to connect them to our Vault and Consul clusters. This allows us to
expose the services that they provide to our other systems, as well as allowing us to
simplify the configuration needed to let those applications connect to other system
components. In an EC2 environment this is as simple as running a Consul and Vault agent,
and setting up DNS routing to use the Consul agent running on localhost.

Fargate makes this challenging due to the set of (reasonable) constraints that it places
on the tasks that it runs. Among these is the fact that it can only use the DNS server
at the VPC level for address resolution. This is due to its use of an Elastic Network
Interface (ENI) for network traffic. It is possible for containers to communicate with
their peer containers in a task group over localhost connections, but since DNS is a
privileged process it is not straightforward to force those queries to rely on a sidecar
in the grouping. Because it is not possible to use Consul as the DNS provider, it
complicates the configuration of Vault agents for locating the server cluster that it
needs to authenticate to.

## The Solution
In order to work around these constraints it is possible to use the AWS Cloud Map
service as a means of registering and discovering services across our
infrastructure. This in conjunction with the
[consul-aws](https://github.com/hashicorp/consul-aws/) application provides a means of
setting up a bi-directional sync of services registered in Consul and services
registered in [AWS Cloud Map](https://aws.amazon.com/cloud-map/). By registering a
cloud-map namespace for each of our VPCs and creating an ECS Fargate task to execute the
consul-aws synchronization, we can maintain a consistent view of what services are
available, regardless of whether they are communicating with the cloud-map DNS or the
Consul DNS.

## Design Challenges
While the use of Consul and AWS Cloud Map provides a low-complexity means of keeping
things in sync, it does provide some friction in how applications are configured. This
is due to the fact that the DNS names for a given service will be different depending on
which system is being queried. For a service being discovered via Consul the query would
be `<service_name>.service.consul`, whereas in AWS Cloud-Map it would be
`<service_name>.<cloud-map_namespace>`. This is largely a manageable problem, since it
will be primarily Fargate processes that need to interact with cloud-map and most other
systems will use Consul, but it will require a clear understanding of which processes
are communicating in which contexts.
