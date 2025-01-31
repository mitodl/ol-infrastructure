import boto3
from transformers import AutoModelForCausalLM, AutoTokenizer


def upload_model_to_s3(model_name, bucket_name, s3_client):
    # Load the model and tokenizer from Hugging Face
    model = AutoModelForCausalLM.from_pretrained(model_name)
    tokenizer = AutoTokenizer.from_pretrained(model_name)

    # Save the model and tokenizer to a local directory
    model.save_pretrained("./model")
    tokenizer.save_pretrained("./model")

    # Upload the model files to S3
    for file in [
        "config.json",
        "pytorch_model.bin",
        "tokenizer_config.json",
        "vocab.txt",
    ]:
        s3_client.upload_file(f"./model/{file}", bucket_name, f"{model_name}/{file}")


def import_model_to_bedrock(model_name, bucket_name):
    # Initialize the S3 client
    s3_client = boto3.client("s3")

    # Upload the model to S3
    upload_model_to_s3(model_name, bucket_name, s3_client)

    # TODO(blarghmatey): Add code to import the model into AWS Bedrock  # noqa: FIX002
    # This part will depend on the specific API and configuration of AWS Bedrock
    # https://github.com/mitodl/ol-infrastructure/issues/2932


if __name__ == "__main__":
    model_name = "gpt2"  # Replace with your desired Hugging Face model name
    bucket_name = "your-s3-bucket-name"  # Replace with your S3 bucket name

    import_model_to_bedrock(model_name, bucket_name)
