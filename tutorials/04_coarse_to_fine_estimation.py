import pyredner
import torch
import scipy
import scipy.ndimage
import numpy as np

# Joint material & camera pose estimation with global illumination
# using coarse to fine estimation

# This time we will learn the following through a Cornell box example.
# - Loading from a Mitsuba scene file
# - Global illumination and glossy/specular materials
# - Coarse to fine estimation with a Gaussian pyramid loss
# - Incorporate box constraints in your optimizer

# In addition to Wavefront obj file, redner also supports loading from a Mitsuba
# scene file. Currently we only support a limited amount of features. In particular
# we only support two kinds of materials: diffuse and roughplastic. Note that the
# "alpha" values in roughplastic is the square root of the roughness. See cbox.xml
# for how a Mitsuba scene file should look like.
# We can load a scene using pyredner.load_mitsuba() utility.
scene = pyredner.load_mitsuba('cbox/cbox.xml')
scene_args = pyredner.RenderFunction.serialize_scene(\
    scene = scene,
    num_samples = 512,
    max_bounces = 6) # Set max_bounces = 6 for global illumination
render = pyredner.RenderFunction.apply
img = render(0, *scene_args)
pyredner.imwrite(img.cpu(), 'results/coarse_to_fine_estimation/target.exr')
pyredner.imwrite(img.cpu(), 'results/coarse_to_fine_estimation/target.png')
target = pyredner.imread('results/coarse_to_fine_estimation/target.exr').numpy()

# Now let's generate an initial guess by perturbing the reference.
# Let's assume we already know the materials except the box.
# The material of the box is stored at scene.materials[0]
material_vars = []
for mi, m in enumerate(scene.materials):
    # if mi <= 1: # Optimize specularities for the first material
    var = torch.tensor([0.5, 0.5, 0.5],
                       device = pyredner.get_device(),
                       requires_grad = True)
    material_vars.append(var)
    m.diffuse_reflectance = pyredner.Texture(var)
        
        # var = torch.tensor([0.1, 0.1, 0.1],
        #                    device = pyredner.get_device(),
        #                    requires_grad = True)
        # material_vars.append(var)
        # m.specular_reflectance = pyredner.Texture(var)
        # var = torch.tensor([0.2],
        #                    device = pyredner.get_device(),
        #                    requires_grad = True)
        # material_vars.append(var)
        # m.roughness = pyredner.Texture(var)

# And let's also slightly perturb the camera pose a bit
position = torch.tensor([0.27, 0.27, -0.85], requires_grad = True)
look_at = torch.tensor([0.27, 0.27, 0.], requires_grad = True)
up = torch.tensor([0.1, 0.9, -0.1], requires_grad = True)
fov = torch.tensor([40.0], requires_grad = True)
cam_vars = [position, look_at, up, fov]
scene.camera = pyredner.Camera(\
    position = 1000.0 * position,
    look_at = 1000.0 * look_at,
    up = up,
    fov = fov,
    clip_near = scene.camera.clip_near,
    resolution = scene.camera.resolution)
# Serialize the scene and render the initial guess
scene_args = pyredner.RenderFunction.serialize_scene(\
    scene = scene,
    num_samples = 512,
    max_bounces = 6)
# img = render(1, *scene_args)
# pyredner.imwrite(img.cpu(), 'results/coarse_to_fine_estimation/init.png')

# Optimize for parameters.
optimizer = torch.optim.Adam(material_vars + cam_vars, lr=5e-3)
# We run a coarse-to-fine estimation here to prevent being trapped in local minimum
# The final resolution is 512x512, but we will start from an 64x64 image
res = [256, 512]
iters = [100, 100]
spp = [8, 4]
bounces = [6, 6]
iter_count = 0
for ri, r in enumerate(res):
    scene.camera.resolution = (r, r)
    # Downsample the target to match our current resolution
    resampled_target = target
    if r != 512:
        resampled_target = scipy.ndimage.interpolation.zoom(\
            target, (r/512.0, r/512.0, 1.0),
            order=1)
    resampled_target = torch.from_numpy(resampled_target)
    if pyredner.get_use_gpu():
        resampled_target = resampled_target.cuda()

    for t in range(iters[ri]):
        print('resolution:', r, ', iteration:', iter_count)
        optimizer.zero_grad()
        # Forward pass: serialize the scene and render the image
        # Need to redefine the camera
        scene.camera = pyredner.Camera(\
            position = 1000 * position,
            look_at = 1000 * look_at,
            up = up,
            fov = fov,
            clip_near = scene.camera.clip_near,
            resolution = scene.camera.resolution)
        scene_args = pyredner.RenderFunction.serialize_scene(\
            scene = scene,
            num_samples = spp[ri],
            max_bounces = bounces[ri]) # increase number of bounces over time
        # Important to use a different seed every iteration, otherwise the result
        # would be biased.
        img = render(iter_count+1, *scene_args)
        # Save the intermediate render.
        img_np = img.data.cpu().numpy()
        img_np = scipy.ndimage.interpolation.zoom(\
            img_np, (512.0/r, 512.0/r, 1.0),
            order=1)
        pyredner.imwrite(torch.from_numpy(img_np),
            'results/coarse_to_fine_estimation/iter_{}.png'.format(iter_count))
        iter_count += 1

        # Compute the loss function. Here we use a Gaussian pyramid loss function
        # We also clamp the difference between -1 and 1 to prevent
        # light source from being dominating the loss function
        diff = (img - resampled_target).clamp(-0.5, 0.5)

        # Now we convolve diff with Gaussian filter and downsample.
        # We use PyTorch's conv2d function and AvgPool2d to achieve this.
        # We need to first define a Gaussian kernel:
        dirac = np.zeros((7,7), dtype = np.float32)
        dirac[3,3] = 1.0
        f = np.zeros([3, 3, 7, 7], dtype = np.float32)
        gf = scipy.ndimage.filters.gaussian_filter(dirac, 1.0)
        f[0, 0, :, :] = gf
        f[1, 1, :, :] = gf
        f[2, 2, :, :] = gf
        f = torch.from_numpy(f)
        m = torch.nn.AvgPool2d(2)
        if pyredner.get_use_gpu():
            f = f.cuda()

        diff_0 = diff.view(1, r, r, 3).permute(0, 3, 2, 1)
        # Convolve then downsample
        diff_1 = m(torch.nn.functional.conv2d(diff_0, f, padding=3))
        diff_2 = m(torch.nn.functional.conv2d(diff_1, f, padding=3))
        diff_3 = m(torch.nn.functional.conv2d(diff_2, f, padding=3))
        loss = diff_0.pow(2).sum() / (r * r) + \
               diff_1.pow(2).sum() / ((r/2.)*(r/2.)) + \
               diff_2.pow(2).sum() / ((r/4.)*(r/4.)) + \
               diff_3.pow(2).sum() / ((r/8.)*(r/8.))
        print('loss:', loss.item())

        # Backpropagate the gradients.
        loss.backward()

        # Take a gradient descent step.
        optimizer.step()
        print('position:', position)
        print('up:', up)

        # Important: the material parameters has hard constraints: the
        # reflectance and roughness cannot be negative. We enforce them here
        # by projecting the values to the boundaries.
        for var in material_vars:
            var.data = var.data.clamp(1e-5, 1.0)
            print(var)

# Render the final result.
scene.camera = pyredner.Camera(\
    position = 1000 * position,
    look_at = 1000 * look_at,
    up = up,
    fov = fov,
    clip_near = scene.camera.clip_near,
    resolution = scene.camera.resolution)
scene_args = pyredner.RenderFunction.serialize_scene(\
    scene = scene,
    num_samples = 512,
    max_bounces = 6)
img = render(302, *scene_args)
# Save the images and differences.
pyredner.imwrite(img.cpu(), 'results/coarse_to_fine_estimation/final.exr')
pyredner.imwrite(img.cpu(), 'results/coarse_to_fine_estimation/final.png')
pyredner.imwrite(torch.abs(target - img).cpu(), 'results/coarse_to_fine_estimation/final_diff.png')

# Convert the intermediate renderings to a video.
from subprocess import call
call(["ffmpeg", "-framerate", "24", "-i",
    "results/coarse_to_fine_estimation/iter_%d.png", "-vb", "20M",
    "results/coarse_to_fine_estimation/out.mp4"])
