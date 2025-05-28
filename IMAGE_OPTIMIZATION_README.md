# Image Optimization API - Frontend Integration Guide

## Overview

Our image optimization service provides on-demand image processing with a Cloudflare-like URL structure. Images are optimized asynchronously and cached for future requests, providing fast delivery and reduced bandwidth usage.

## Base URL Structure

```
GET /api/optimize/{transformations}/{s3_key}
```

- `{transformations}` - Optimization parameters (see below)
- `{s3_key}` - Path to the original image in S3

## Transformation Parameters

### Auto Optimization
```
/api/optimize/auto/uploads/user123/image.jpg
```
- Automatically chooses optimal format and settings
- Default: WebP format, 85% quality, max 1920x1080

### Manual Transformations
Combine multiple parameters with commas:

```
/api/optimize/w_800,h_600,q_80,f_webp/uploads/user123/image.jpg
```

**Available Parameters:**
- `w_{width}` - Maximum width (e.g., `w_800`)
- `h_{height}` - Maximum height (e.g., `h_600`) 
- `q_{quality}` - Quality 1-100 (e.g., `q_80`)
- `f_{format}` - Output format: `webp`, `jpeg`, `png` (e.g., `f_webp`)

### Examples
```
# Resize to max 1200px width
/api/optimize/w_1200/assets/banner.png

# High quality thumbnail
/api/optimize/w_300,h_300,q_95/profile-pics/avatar.jpg

# Optimized for web
/api/optimize/w_1920,q_75,f_webp/gallery/photo.jpg

# Auto-optimize
/api/optimize/auto/uploads/document.png
```

## Frontend Implementation

### React/Next.js Example

```typescript
interface ImageOptimizerProps {
  src: string; // S3 key path
  transformations?: string;
  alt: string;
  className?: string;
  fallbackToOriginal?: boolean;
}

const OptimizedImage: React.FC<ImageOptimizerProps> = ({
  src,
  transformations = "auto",
  alt,
  className,
  fallbackToOriginal = true
}) => {
  const [imageSrc, setImageSrc] = useState<string>("");
  const [isOptimized, setIsOptimized] = useState<boolean>(false);

  useEffect(() => {
    const optimizedUrl = `${process.env.NEXT_PUBLIC_API_URL}/api/optimize/${transformations}/${src}`;
    setImageSrc(optimizedUrl);
    setIsOptimized(true);
  }, [src, transformations]);

  const handleError = () => {
    if (fallbackToOriginal && isOptimized) {
      // Fallback to original image URL
      const originalUrl = `${process.env.NEXT_PUBLIC_S3_URL}/${src}`;
      setImageSrc(originalUrl);
      setIsOptimized(false);
    }
  };

  return (
    <img
      src={imageSrc}
      alt={alt}
      className={className}
      onError={handleError}
      loading="lazy"
    />
  );
};

// Usage
<OptimizedImage 
  src="uploads/user123/photo.jpg"
  transformations="w_800,h_600,q_80,f_webp"
  alt="User photo"
  className="rounded-lg"
/>
```

### Utility Functions

```typescript
// Utility to generate optimized image URLs
export const getOptimizedImageUrl = (
  imagePath: string, 
  transformations: string = "auto"
): string => {
  const baseUrl = process.env.NEXT_PUBLIC_API_URL || "";
  return `${baseUrl}/api/optimize/${transformations}/${imagePath}`;
};

// Responsive image helper
export const getResponsiveImageUrls = (imagePath: string) => ({
  thumbnail: getOptimizedImageUrl(imagePath, "w_300,h_300,q_85,f_webp"),
  small: getOptimizedImageUrl(imagePath, "w_600,q_80,f_webp"),
  medium: getOptimizedImageUrl(imagePath, "w_1200,q_80,f_webp"),
  large: getOptimizedImageUrl(imagePath, "w_1920,q_75,f_webp"),
  original: getOptimizedImageUrl(imagePath, "auto")
});

// Usage in component
const imageUrls = getResponsiveImageUrls("gallery/sunset.jpg");

<picture>
  <source 
    media="(max-width: 640px)" 
    srcSet={imageUrls.small} 
  />
  <source 
    media="(max-width: 1024px)" 
    srcSet={imageUrls.medium} 
  />
  <img 
    src={imageUrls.large} 
    alt="Sunset" 
    loading="lazy" 
  />
</picture>
```

### Vue.js Example

```vue
<template>
  <img 
    :src="optimizedSrc"
    :alt="alt"
    :class="className"
    @error="handleError"
    loading="lazy"
  />
</template>

<script setup lang="ts">
interface Props {
  src: string;
  transformations?: string;
  alt: string;
  className?: string;
}

const props = withDefaults(defineProps<Props>(), {
  transformations: 'auto'
});

const optimizedSrc = computed(() => {
  const baseUrl = import.meta.env.VITE_API_URL;
  return `${baseUrl}/api/optimize/${props.transformations}/${props.src}`;
});

const handleError = (event: Event) => {
  const img = event.target as HTMLImageElement;
  const originalUrl = `${import.meta.env.VITE_S3_URL}/${props.src}`;
  img.src = originalUrl;
};
</script>
```

## Environment Configuration

Set these environment variables in your frontend:

```env
# Next.js (.env.local)
NEXT_PUBLIC_API_URL=https://your-api-domain.com
NEXT_PUBLIC_S3_URL=https://your-bucket.s3.region.amazonaws.com

# Vite (.env)
VITE_API_URL=https://your-api-domain.com
VITE_S3_URL=https://your-bucket.s3.region.amazonaws.com
```

## Performance Best Practices

### 1. Lazy Loading
Always use `loading="lazy"` for images below the fold:

```html
<img src="/api/optimize/auto/image.jpg" loading="lazy" alt="Description" />
```

### 2. Responsive Images
Use different transformations for different screen sizes:

```typescript
const getResponsiveUrl = (path: string, width: number) => 
  `/api/optimize/w_${width},q_80,f_webp/${path}`;

// Mobile: 400px, Tablet: 800px, Desktop: 1200px
<img 
  srcSet={`
    ${getResponsiveUrl(imagePath, 400)} 400w,
    ${getResponsiveUrl(imagePath, 800)} 800w,
    ${getResponsiveUrl(imagePath, 1200)} 1200w
  `}
  sizes="(max-width: 640px) 400px, (max-width: 1024px) 800px, 1200px"
  src={getResponsiveUrl(imagePath, 800)}
  alt="Responsive image"
/>
```

### 3. Preload Critical Images
For above-the-fold images:

```html
<link 
  rel="preload" 
  as="image" 
  href="/api/optimize/w_1200,q_80,f_webp/hero-banner.jpg"
/>
```

### 4. Format Selection
- **WebP**: Best for most use cases (smaller file size, good quality)
- **JPEG**: For photos when WebP isn't supported
- **PNG**: For images with transparency or simple graphics

## Error Handling

The API returns appropriate HTTP status codes:

- `200` - Success (redirects to optimized image)
- `302` - Redirect to image URL
- `404` - Original image not found
- `500` - Server error during optimization

```typescript
const OptimizedImage = ({ src, ...props }) => {
  const [imageUrl, setImageUrl] = useState("");
  const [hasError, setHasError] = useState(false);

  useEffect(() => {
    const optimizedUrl = `/api/optimize/auto/${src}`;
    
    // Pre-check if optimization endpoint is working
    fetch(optimizedUrl, { method: 'HEAD' })
      .then(response => {
        if (response.ok) {
          setImageUrl(optimizedUrl);
        } else {
          throw new Error('Optimization failed');
        }
      })
      .catch(() => {
        setImageUrl(`/original/${src}`); // Fallback to original
        setHasError(true);
      });
  }, [src]);

  return (
    <img 
      src={imageUrl} 
      onError={() => setHasError(true)}
      {...props} 
    />
  );
};
```

## Migration from Other Services

### From Cloudflare Images
Replace Cloudflare URLs with our service:

```typescript
// Before (Cloudflare)
const cloudflareUrl = `https://imagedelivery.net/account/w=800,h=600,q=80,f=webp/${imageId}`;

// After (Our service)
const optimizedUrl = `/api/optimize/w_800,h_600,q_80,f_webp/${s3Key}`;
```

### From ImageKit/Uploadcare
Similar transformation syntax makes migration straightforward:

```typescript
// Generic transformation mapper
const mapTransformations = (width?: number, height?: number, quality?: number, format?: string) => {
  const params = [];
  if (width) params.push(`w_${width}`);
  if (height) params.push(`h_${height}`);
  if (quality) params.push(`q_${quality}`);
  if (format) params.push(`f_${format}`);
  return params.join(',') || 'auto';
};
```

## Caching Behavior

- **First request**: Image is optimized asynchronously, URL is returned immediately
- **Subsequent requests**: Cached optimized image is served instantly
- **Cache key**: Based on S3 key + transformation parameters
- **Storage**: Optimized images are stored in S3 with deterministic paths

## Troubleshooting

### Common Issues

1. **404 errors**: Ensure the original S3 key path is correct
2. **Slow initial load**: First optimization takes 5-10 seconds, subsequent loads are instant
3. **Format not supported**: Stick to common formats (JPEG, PNG, WebP)
4. **Large images timing out**: Consider pre-processing very large images

### Debug Mode
Add query parameter for debugging:

```typescript
const debugUrl = `/api/optimize/auto/${imagePath}?debug=true`;
```

## Support

For technical issues or questions about the image optimization API, please:

1. Check server logs for optimization errors
2. Verify S3 key paths are correct
3. Test with simple transformations first (`auto` or `w_800`)
4. Contact the backend team with specific error details

---

**Note**: This service is designed to be a drop-in replacement for Cloudflare Images. The URL structure is intentionally similar to make migration easier.