output "public_ip" {
  value = aws_lightsail_static_ip.app.ip_address
}

output "instance_name" {
  value = aws_lightsail_instance.app.name
}

output "bucket_name" {
  value = aws_lightsail_bucket.data.name
}
