docker build -t comfydeploy-api . 
docker run -p 8000:8000 comfydeploy-api

CloudFront CDN
Set COMPANY_CLOUDFRONT_DOMAIN to your company CloudFront distribution domain (no scheme) to serve public optimized images via CDN. Custom buckets can optionally enable CloudFront per user settings (use_cloudfront=true and cloudfront_domain set). Private images remain served via presigned S3 URLs.
