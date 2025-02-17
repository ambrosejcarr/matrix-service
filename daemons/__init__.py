"""
This folder contains AWS Lambda handlers for executing a map reduce job to process a matrix query.
Specifically, the map reduce job performs a distributed filter and merge on input .zarr expression matrices,
writes the resultant .zarr expression matrix to S3 and returns that S3 location to the client.

Four lambdas define the map reduce interface for processing matrix queries:

driver  - Initialize a map reduce job and spawn a mapper lambda for each bundle uuid/expression matrix file
mapper  - Divide input expression matrix into chunks (row subset), and spawn a worker lambda for each chunk
worker  - Apply user-defined filter query on chunk, write partial results to S3
reducer - Combine partial results into final .zarr file in S3, return S3 location
"""
