# A lambda function to replicate AWS DocumentDB change streams events

This CloudFormation can be used to deploy a lambda to replicate AWS DocumentDB change streams events to ElasticSearch, Amazon Managed Stream for Kafka (or any other Apache Kafka distro) SNS, or S3. S3 replication is done in micro-batches and the rest of the integration are near real-time.  

The lambda function is scheduled using a CloudWatch rule. Everytime the function is triggered it queries the DocumentDB change stream for a specific collection and publish them to any of the targets selected. 

Since documentDB are private resources that run in one VPC, there should be as many lambdas as vpcs with clusters to be monitored. Lambdas run within VPC therefore it is necessary that it runs in at least one private subnet that can reach internet using a either a NAT gateway or privatelinks for the services it needs to integrate with. 

Lambda uses documentDB credentials and password is encrypted using KMS.    

Apache Kafka integration requires kafka-python==2.0.1 library which is commented in the requirements.txt config file by default. If needed, uncomment it from that file.  

By default, this lambda will publish events to SNS. All other integration code is commented, if you required to enabled to other integrations, just uncomment code related to each. 

# How to install
0. Enable change streams at collection level. Follow instructions given here: https://docs.aws.amazon.com/documentdb/latest/developerguide/change-streams.html
1. Create a python virtualenv and move files in app folder to it. Install dependencies (uncomment kafka-python if required) and zip it (Python runtime used is 3.7.4).
2. Create a second python virtualenv and move files at https://github.com/herbertgoto/lambda-envvar-encrypter/tree/master/app to it. Install dependencies and zip it (Python runtime used is 3.7.4)
3. Upload both zip files to an AWS S3 bucket.
4. Run the CloudFormation template "documentdb_replicator.yaml".
    1. You will need to fill in the parameter for S3 bucket name.
    2. You will need to fill in the parameter for S3 key for both zip files.
    3. You will need to fill in the parameter for the subnets where the lambda will run.
    4. You will need to fill in the parameter for the security groups where the lambda will run.
    5. You will need to fill in the parameter for the documentDB credentials. 
    6. You will need to fill in the parameter for the monitoring thresholds. 
    7. You will need to fill in the parameter for the tag that will identify the clusters to monitor. Lambda just checks if the value is part of a tag.   
5. Cloudwatch rule is schedule to run every 5 minutes and is disabled by design, modify according to your needs and enable it. 

If you alread have the KMS CMK, you can comment the KMS Resource and instead enable it as a CFN parameter (uncomment this part in the CFN). Review the policies and the custom resource that make use of the KMS CMK and make the changes to use the parameter instead.  