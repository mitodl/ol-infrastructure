# Open Learning Concourse Wrapper
At MIT Open Learning we use Concourse as a CI/CD framework, as well as a tool for more
general automation (e.g. scheduled tasks). While the underlying engine is very powerful
and flexible, the interface for controlling it is cumbersome. Pure YAML files allow for
language agnostic integration, but pose challenges around logic reuse, discovery of
existing solutions, and any logic or control flow in how the pipelines should be
constructed.

This package provides:
- A set of Pydantic models that represent the data structures used to construct a Concourse pipeline
- A set of library functions that provide reusable components for common Pipeline concerns (e.g. resource types, resource definitions, etc.)
